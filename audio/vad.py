"""Voice-activity noise gate (webrtcvad, settings.noise_filter).

The RMS silence gate in audio/capture.py is a pure loudness check: static
hiss, mains hum or other "tech" noise from a muted mixer channel sits above
SILENCE_THRESHOLD, reaches the STT models and comes back as hallucinated
sentences. webrtcvad classifies 30 ms frames by spectral shape instead, so
stationary noise is rejected even when it is loud.

Two consumers:

* ``has_speech`` — whole-segment check for the segmented live pipeline and
  batch mode: skip the STT call entirely when a non-silent segment contains
  no speech.
* ``StreamNoiseGate`` — streaming mode cannot simply drop audio (the engines'
  timing, keepalive and utterance detection expect a continuous feed), so
  sustained non-speech is replaced with digital silence instead: the
  connection stays alive and billed, but the server-side VAD hears true
  silence instead of static and stops inventing turns.

webrtcvad is imported lazily and every entry point degrades to "everything
is speech" when it is unavailable — the noise filter is an enhancement; its
absence must never take down the pipeline.

Known limit (measured empirically): very loud broadband static (peak around
-20 dBFS) is classified as speech by webrtcvad. Typical muted-channel noise
floors are far quieter, but the gate is a filter, not a guarantee.
"""

from __future__ import annotations

from collections import deque

import numpy as np

from config import (
    FS,
    VAD_AGGRESSIVENESS,
    VAD_DECISION_MAX_BOOST,
    VAD_DECISION_TARGET_PEAK,
    VAD_MIN_SPEECH_RATIO,
    VAD_STREAM_HANGOVER_SECONDS,
    VAD_STREAM_OPEN_RATIO,
    VAD_STREAM_WINDOW_SECONDS,
)
from utils.logging import log

_FRAME_MS = 30  # webrtcvad accepts 10/20/30 ms frames

_unavailable_logged = False


def _new_vad():
    """A fresh webrtcvad.Vad, or None when the package is unavailable.

    Per-use instances: the VAD keeps adaptive internal state, so sharing one
    between threads (or across unrelated segments) would race/leak state.
    """
    global _unavailable_logged
    try:
        import webrtcvad

        return webrtcvad.Vad(VAD_AGGRESSIVENESS)
    except Exception as e:
        if not _unavailable_logged:
            _unavailable_logged = True
            log(
                f"webrtcvad unavailable — noise filter inactive: {e}",
                level="WARNING",
            )
        return None


def _to_vad_rate(pcm: np.ndarray, rate: int) -> tuple[np.ndarray | None, int]:
    """Map int16 PCM to a webrtcvad-supported rate (8/16/32/48 kHz).

    The 24 kHz OpenAI Realtime capture is decimated to 8 kHz (mean of 3 —
    a crude low-pass, plenty for a speech/non-speech decision). Returns
    (None, 0) for rates that can't be mapped: callers treat that as speech.
    """
    if rate in (8000, 16000, 32000, 48000):
        return pcm, rate
    if rate % 8000 == 0:
        k = rate // 8000
        n = pcm.size // k * k
        return pcm[:n].reshape(-1, k).mean(axis=1).astype(np.int16), 8000
    return None, 0


def _boost_for_decision(pcm: np.ndarray) -> np.ndarray:
    """Bounded normalization of the VAD *decision* copy.

    webrtcvad stops detecting real speech below ~-46 dBFS peak, so a quiet
    mic (low interface gain) made the gate classify everything as non-speech
    and starve the pipeline. The copy judged by the VAD is boosted toward
    VAD_DECISION_TARGET_PEAK (capped at VAD_DECISION_MAX_BOOST) — the audio
    actually fed onward is never modified. The target sits ≥4 dB below the
    earliest measured noise flip (see config.py), so quiet hiss/hum cannot
    be boosted into a false gate-open.
    """
    peak = int(np.abs(pcm).max()) if pcm.size else 0
    if peak == 0:
        return pcm
    boost = min(VAD_DECISION_MAX_BOOST, VAD_DECISION_TARGET_PEAK * 32768.0 / peak)
    if boost <= 1.0:
        return pcm
    return np.clip(pcm.astype(np.float32) * boost, -32768, 32767).astype(np.int16)


def _frame_decisions(pcm: np.ndarray, rate: int, vad) -> list[bool]:
    """webrtcvad speech decision per 30 ms frame; trailing partial dropped."""
    frame_len = int(rate * _FRAME_MS / 1000)
    n = pcm.size // frame_len
    if n == 0:
        return []
    data = pcm[: n * frame_len].tobytes()
    step = frame_len * 2  # bytes per int16 frame
    return [vad.is_speech(data[i * step : (i + 1) * step], rate) for i in range(n)]


def has_speech(audio: np.ndarray, sample_rate: int = FS) -> bool:
    """True when a segment contains speech (or the check cannot run).

    ``audio`` is float32 in [-1, 1], the segmented/batch working format.
    Returns True — gate open — whenever webrtcvad is unavailable or the
    sample rate is unsupported: the filter must only ever remove known
    non-speech, never block the pipeline.
    """
    vad = _new_vad()
    if vad is None:
        return True
    try:
        pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
        pcm, rate = _to_vad_rate(pcm, sample_rate)
        if pcm is None:
            return True
        decisions = _frame_decisions(_boost_for_decision(pcm), rate, vad)
        if not decisions:
            return True  # shorter than one frame — nothing to judge
        return sum(decisions) / len(decisions) >= VAD_MIN_SPEECH_RATIO
    except Exception as e:
        log(f"Noise filter check failed — treating as speech: {e}", level="WARNING")
        return True


class StreamNoiseGate:
    """Replaces sustained non-speech in a live PCM feed with digital silence.

    Chunks are int16 mono little-endian bytes at ``sample_rate`` (the shape
    the streaming feeder sends). The open/close decision uses the speech
    ratio over a rolling ~1 s frame window, so a single false-positive frame
    on hiss cannot reopen the gate; the gate only closes after
    VAD_STREAM_HANGOVER_SECONDS without speech, so normal speech pauses are
    never touched. The chunk containing a speech onset passes through in
    full (the decision is made before forwarding), so at most the VAD's own
    detection lag is lost.

    Time is counted in samples fed, not wall clock — deterministic and
    unaffected by feeder-thread scheduling. Pass-through when webrtcvad is
    unavailable.
    """

    def __init__(self, sample_rate: int):
        self._rate = sample_rate
        self._vad = _new_vad()
        self._hangover_samples = int(VAD_STREAM_HANGOVER_SECONDS * sample_rate)
        window_frames = max(1, int(VAD_STREAM_WINDOW_SECONDS * 1000 / _FRAME_MS))
        self._frames: deque[bool] = deque(maxlen=window_frames)
        # Gate starts open: never eat the first words of a session while the
        # window fills.
        self._samples_since_speech = 0
        self._zeroing = False

    def process(self, chunk: bytes) -> bytes:
        # Pass through anything that isn't well-formed int16 PCM — the gate
        # must only ever silence known non-speech, never break the feed.
        if self._vad is None or not chunk or len(chunk) % 2:
            return chunk
        try:
            pcm = np.frombuffer(chunk, dtype=np.int16)
            vad_pcm, vad_rate = _to_vad_rate(pcm, self._rate)
            if vad_pcm is None:
                return chunk
            self._frames.extend(
                _frame_decisions(_boost_for_decision(vad_pcm), vad_rate, self._vad)
            )
        except Exception as e:
            # Disable for the rest of the session instead of failing (and
            # logging) again on every 200 ms chunk.
            self._vad = None
            log(
                f"NOISE-GATE error — passing audio through: {e}",
                level="WARNING",
            )
            return chunk
        ratio = (
            sum(self._frames) / len(self._frames) if self._frames else 0.0
        )
        if ratio >= VAD_STREAM_OPEN_RATIO:
            self._samples_since_speech = 0
            if self._zeroing:
                self._zeroing = False
                log("NOISE-GATE Speech resumed — gate open", level="DEBUG")
            return chunk
        self._samples_since_speech += pcm.size
        if self._samples_since_speech <= self._hangover_samples:
            return chunk
        if not self._zeroing:
            self._zeroing = True
            log(
                "NOISE-GATE Sustained non-speech — feeding silence",
                level="DEBUG",
            )
        return bytes(len(chunk))
