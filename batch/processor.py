"""Run a pre-recorded audio/video file through the live pipeline into an SRT.

Mirrors app_controller's per-segment flow (transcription → RAG → translation)
but linearly over a file. Unlike the live pipeline (and unlike the earlier
blind fixed-clock version of this module), segmentation is **silence-based**:
the file is split on its own pauses, so a segment boundary always falls in
silence. That fixes two failure modes of blind fixed-length chunking:

  * words are no longer cut in half at an arbitrary 12s boundary, and
  * a short utterance surrounded by a pause is no longer thrown away because
    the fixed block it happened to land in was "mostly silent".

Silence between segments still costs no API call. Segment start/end positions
become the subtitle timestamps.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable

import numpy as np
import scipy.io.wavfile as wavfile

from audio.vad import has_speech
from batch.srt_writer import SrtEntry, write_srt
from batch.text_writer import write_text
from config import (
    BATCH_MAX_SEGMENT_SECONDS,
    BATCH_MAX_SILENCE_KEEP_SECONDS,
    BATCH_MIN_SEGMENT_SECONDS,
    BATCH_MIN_SILENCE_GAP_SECONDS,
    BATCH_MIN_STANDALONE_SECONDS,
    CONTEXT_RECENT_RAW_COUNT,
    FS,
    SILENCE_THRESHOLD,
)
from providers import (
    get_transcription_model_chain,
    get_transcription_model_chain_for,
    get_transcription_provider,
    get_transcription_provider_for,
)
from translation.stt import maybe_arabic_retranscription, transcribe_with_fallback
from translation.translator import translate_text
from utils.history import batch_srt_path, write_batch_record
from utils.logging import log
from utils.settings import (
    get_source_language_code,
    get_target_language_code,
    load_settings,
)

# RMS analysis window for the voice-activity segmentation (matches is_silence).
_FRAME_MS = 50
# Forced splits (unbroken speech over the cap) cut at the quietest *sustained*
# stretch of this many frames — a single 50 ms minimum can land mid-word and
# garble the text on both sides of the cut.
_SPLIT_WINDOW_FRAMES = 6  # 300 ms
# Tail of the previous transcription passed as the STT prompt for continuity.
# Kept short so one bad segment cannot poison the rest of the session.
_PROMPT_TAIL_CHARS = 200
# Live-observed failure mode: on non-speech audio (music/noise) the model can
# echo the continuity prompt back verbatim instead of transcribing. A long
# exact echo is never real speech; short repeats (dhikr, takbir) must pass.
_PROMPT_ECHO_MIN_CHARS = 80


class FfmpegNotFoundError(RuntimeError):
    """Raised when a non-WAV input needs ffmpeg but none is installed."""


def _find_ffmpeg() -> str | None:
    """Return the path to ffmpeg, or None if not found.

    On Windows, the process PATH may be stale (e.g. ffmpeg installed via
    WinGet/Chocolatey after VS Code was opened). As a fallback we read the
    current User and System PATH directly from the registry so the user
    doesn't have to restart VS Code after installing ffmpeg.
    """
    path = shutil.which("ffmpeg")
    if path:
        return path
    # App-managed copy downloaded via the batch card (utils/ffmpeg_download)
    try:
        from utils.ffmpeg_download import bundled_ffmpeg_path

        bundled = bundled_ffmpeg_path()
        if os.path.isfile(bundled):
            return bundled
    except Exception:
        pass
    if sys.platform != "win32":
        return None
    try:
        import winreg

        hives = [
            (winreg.HKEY_CURRENT_USER, r"Environment"),
            (
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
            ),
        ]
        for hive, subkey in hives:
            try:
                with winreg.OpenKey(hive, subkey) as key:
                    raw, _ = winreg.QueryValueEx(key, "Path")
                for directory in raw.split(";"):
                    directory = os.path.expandvars(directory.strip())
                    candidate = os.path.join(directory, "ffmpeg.exe")
                    if os.path.isfile(candidate):
                        return candidate
            except OSError:
                pass
    except Exception:
        pass
    return None


def is_ffmpeg_available() -> bool:
    return _find_ffmpeg() is not None


def _extract_audio(input_path: str, wav_path: str) -> None:
    """Convert any audio/video file to 16 kHz mono WAV via ffmpeg."""
    ffmpeg_path = _find_ffmpeg() or "ffmpeg"
    cmd = [
        ffmpeg_path,
        "-y",
        "-i",
        input_path,
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(FS),
        "-f",
        "wav",
        wav_path,
    ]
    # CREATE_NO_WINDOW: don't flash a console window from the GUI on Windows
    creationflags = 0x08000000 if sys.platform == "win32" else 0
    result = subprocess.run(
        cmd, capture_output=True, text=True, creationflags=creationflags
    )
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-1:]
        raise RuntimeError(f"ffmpeg failed: {tail[0] if tail else 'unknown error'}")


def _to_float32(data: np.ndarray) -> np.ndarray:
    """Normalize WAV samples to mono float32 in [-1, 1]."""
    if data.ndim > 1:
        data = data.mean(axis=1)
    if np.issubdtype(data.dtype, np.integer):
        return data.astype(np.float32) / float(np.iinfo(data.dtype).max)
    return data.astype(np.float32)


def _normalize(audio: np.ndarray) -> np.ndarray:
    """Peak-normalize so the silence threshold behaves consistently across
    recordings of different gain.

    Guarded: a (near-)silent file is left untouched so we don't amplify the
    noise floor into false speech. A normal recording is only ever scaled up
    to a 0.9 peak, which cannot introduce clipping.
    """
    if audio.size == 0:
        return audio
    peak = float(np.max(np.abs(audio)))
    if peak > 0.02:
        return audio * (0.9 / peak)
    return audio


def _load_audio(input_path: str) -> np.ndarray:
    """Load a file's audio as mono float32 at FS, converting via ffmpeg
    unless it is already a WAV at the pipeline sample rate."""
    if input_path.lower().endswith(".wav"):
        try:
            rate, data = wavfile.read(input_path)
            if rate == FS:
                return _normalize(_to_float32(data))
        except Exception as e:
            log(f"BATCH Direct WAV read failed, using ffmpeg: {e}", level="DEBUG")

    if not is_ffmpeg_available():
        raise FfmpegNotFoundError(
            "ffmpeg is required to convert this file to 16 kHz WAV."
        )
    with tempfile.TemporaryDirectory() as tmp_dir:
        wav_path = os.path.join(tmp_dir, "audio.wav")
        _extract_audio(input_path, wav_path)
        _, data = wavfile.read(wav_path)
    return _normalize(_to_float32(data))


def _frame_rms(audio: np.ndarray, frame_len: int) -> np.ndarray:
    """Per-frame RMS over non-overlapping ``frame_len``-sample windows."""
    n = audio.size // frame_len
    if n == 0:
        return np.array([], dtype=np.float32)
    frames = audio[: n * frame_len].reshape(n, frame_len)
    return np.sqrt(np.mean(frames**2, axis=1))


def _segment_speech(audio: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Split audio into contiguous, gap-absorbing segments on its own pauses.

    Returns ``(audio_start, audio_end, disp_start, disp_end)`` per segment, all
    in samples: ``audio_*`` is the slice to transcribe, ``disp_*`` the speech
    extent used for subtitle timing.

    Unlike a pure speech-extraction VAD, segments are **contiguous**: a pause up
    to ``BATCH_MAX_SILENCE_KEEP_SECONDS`` is absorbed (the cut falls at its
    centre) rather than dropped, so quiet speech that trails or leads a phrase
    is never lost at a boundary. Only genuinely long silence is trimmed. Cuts
    still land in pauses (no word split); an unbroken run over the cap is split
    at its quietest interior frame; runs below the minimum are dropped as noise.
    """
    frame_len = max(1, int(_FRAME_MS / 1000 * FS))
    rms = _frame_rms(audio, frame_len)
    if rms.size == 0:
        return []

    speech = rms >= SILENCE_THRESHOLD
    merge_gap = max(1, round(BATCH_MIN_SILENCE_GAP_SECONDS * FS / frame_len))
    keep = max(2, round(BATCH_MAX_SILENCE_KEEP_SECONDS * FS / frame_len))
    max_frames = max(1, round(BATCH_MAX_SEGMENT_SECONDS * FS / frame_len))
    min_frames = max(1, round(BATCH_MIN_SEGMENT_SECONDS * FS / frame_len))
    half = keep // 2
    n_frames = len(speech)

    # 1) Speech runs, merging micro-pauses (< merge_gap) into one block.
    runs: list[list[int]] = []
    start: int | None = None
    for idx, is_speech in enumerate(speech):
        if is_speech and start is None:
            start = idx
        elif not is_speech and start is not None:
            runs.append([start, idx])
            start = None
    if start is not None:
        runs.append([start, n_frames])
    if not runs:
        return []
    blocks: list[list[int]] = [runs[0]]
    for s, e in runs[1:]:
        if s - blocks[-1][1] < merge_gap:
            blocks[-1][1] = e
        else:
            blocks.append([s, e])
    blocks = [b for b in blocks if b[1] - b[0] >= min_frames]
    if not blocks:
        return []

    # 1b) A short block transcribed alone tends to hallucinate ("giggle"-class
    #     output), so absorb it into its neighbor when the pause between them
    #     is small enough to keep anyway. Genuinely isolated short utterances
    #     stay standalone.
    min_standalone = max(1, round(BATCH_MIN_STANDALONE_SECONDS * FS / frame_len))
    merged_blocks: list[list[int]] = [blocks[0]]
    for b in blocks[1:]:
        prev = merged_blocks[-1]
        either_short = (
            b[1] - b[0] < min_standalone or prev[1] - prev[0] < min_standalone
        )
        if either_short and b[0] - prev[1] <= keep:
            prev[1] = b[1]
        else:
            merged_blocks.append(b)
    blocks = merged_blocks

    # 2) Contiguous segments: absorb a gap <= keep (cut at its centre), else
    #    trim to `half` frames of silence on each side. disp = speech extent.
    segs: list[tuple[int, int, int, int]] = []
    for i, (bs, be) in enumerate(blocks):
        if i == 0:
            a_start = max(0, bs - half)
        else:
            prev_be = blocks[i - 1][1]
            gap = bs - prev_be
            a_start = (prev_be + bs) // 2 if gap <= keep else bs - half
        if i == len(blocks) - 1:
            a_end = min(n_frames, be + half)
        else:
            next_bs = blocks[i + 1][0]
            gap = next_bs - be
            a_end = (be + next_bs) // 2 if gap <= keep else be + half
        segs.append((a_start, a_end, bs, be))

    # 3) Cap over-long segments, splitting at the quietest interior frame.
    capped: list[tuple[int, int, int, int]] = []
    for a_s, a_e, d_s, d_e in segs:
        if a_e - a_s <= max_frames:
            capped.append((a_s, a_e, d_s, d_e))
            continue
        s = a_s
        while a_e - s > max_frames:
            lo = s + max(min_frames, int(max_frames * 0.6))
            # Never leave a tail shorter than the standalone minimum behind:
            # a forced split must not create the tiny hallucination-prone
            # segment the short-block merge (1b) exists to prevent.
            hi = max(lo + 1, min(s + max_frames, a_e - min_standalone))
            # Cut at the quietest *sustained* stretch, not a single frame —
            # a lone 50 ms minimum can land mid-word and garble both sides.
            win = max(1, min(_SPLIT_WINDOW_FRAMES, hi - lo))
            means = np.convolve(rms[lo:hi], np.ones(win) / win, mode="valid")
            split = lo + int(np.argmin(means)) + win // 2 + 1
            # Interior cuts are all speech, but the first piece may still
            # carry the absorbed leading silence and the last piece the
            # trailing one — keep the display extent on the actual speech.
            capped.append((s, split, max(d_s, s), split))
            s = split
        capped.append((s, a_e, max(d_s, s), min(d_e, a_e)))

    # 4) Frames → samples, clamped.
    result: list[tuple[int, int, int, int]] = []
    for a_s, a_e, d_s, d_e in capped:
        a_start_smp = a_s * frame_len
        a_end_smp = min(a_e * frame_len, audio.size)
        d_start_smp = max(a_start_smp, min(d_s * frame_len, a_end_smp))
        d_end_smp = max(d_start_smp, min(d_e * frame_len, a_end_smp))
        result.append((a_start_smp, a_end_smp, d_start_smp, d_end_smp))
    return result


def _segment_to_wav_bytes(segment: np.ndarray) -> bytes:
    buf = io.BytesIO()
    wavfile.write(buf, FS, (segment * 32767).astype(np.int16))
    return buf.getvalue()


def output_path_for(input_path: str, target_language: str, ext: str = "srt") -> str:
    """Output path next to the source file, tagged with the target language."""
    stem, _ = os.path.splitext(input_path)
    code = get_target_language_code(target_language)
    return f"{stem}.{code}.{ext}" if code else f"{stem}.{ext}"


def process_file(
    input_path: str,
    progress_callback: Callable[[int, int], None] | None = None,
    cancel_event: threading.Event | None = None,
    transcription_provider: str | None = None,
    transcription_model: str | None = None,
    translation_provider: str | None = None,
    translation_model: str | None = None,
    source_language: str | None = None,
    target_language: str | None = None,
    output_format: str = "srt",
    bilingual_srt: bool = False,
) -> str | None:
    """Process a recording into a translated SRT and/or text transcript.

    Args:
        input_path: Audio or video file (anything ffmpeg can decode).
        progress_callback: Called with (segments_done, segments_total).
        cancel_event: Set to abort; nothing is written then.
        transcription_provider: Explicit STT provider id; None uses settings
            (Deepgram maps to its OpenAI fallback — batch is segmented).
        transcription_model: Explicit STT model to lead the fallback chain.
        translation_provider: Explicit translation provider id; None uses
            the configured ai_provider.
        translation_model: Explicit translation model; None uses settings.
        source_language / target_language: Explicit language names for this run
            (the batch card picks them independently of the live app); None
            uses settings.
        output_format: "srt", "txt" or "both"; unknown values mean "srt".
        bilingual_srt: When True, each SRT block carries the original
            transcription above the translation (any language pair; same-
            language segments render a single line). No effect on the .txt.

    Returns:
        Path of the written .srt (or .txt for txt-only runs), or None when
        cancelled.
    """
    settings = load_settings()
    src_language = source_language or settings.source_language
    tgt_language = target_language or settings.target_language
    lang_code = get_source_language_code(src_language)
    if output_format not in ("srt", "txt", "both"):
        output_format = "srt"
    noise_filter = settings.noise_filter

    audio = _load_audio(input_path)
    segments = _segment_speech(audio)
    total = len(segments)
    log(
        f"BATCH {os.path.basename(input_path)}: {total} speech segments",
        level="INFO",
    )

    # Resolve the STT provider + model chain (per-run override or settings).
    if transcription_provider:
        stt_provider = get_transcription_provider_for(transcription_provider)
        models_to_try = get_transcription_model_chain_for(
            transcription_provider, transcription_model
        )
    else:
        stt_provider = get_transcription_provider()
        models_to_try = get_transcription_model_chain()
        if transcription_model:
            models_to_try = [
                transcription_model,
                *[m for m in models_to_try if m != transcription_model],
            ]

    entries: list[SrtEntry] = []
    # (start_seconds, transcription, translation) for the in-app batch record
    records: list[tuple[float, str, str]] = []
    recent: list[str] = []  # rolling raw context (no async summarizer in batch)
    prev_tail = ""  # tail of the last transcription, STT prompt for continuity

    for i, (a_start, a_end, d_start, d_end) in enumerate(segments):
        if cancel_event is not None and cancel_event.is_set():
            log("BATCH Cancelled.", level="INFO")
            return None

        chunk = audio[a_start:a_end]
        # Timestamps track the detected speech, not the (padded) audio window.
        start_s = d_start / FS
        end_s = d_end / FS

        # Loud but not speech (static, hum): the RMS segmentation can't tell
        # — STT would hallucinate sentences from it. Caveat: a file with NO
        # speech at all has its noise floor peak-normalized up, which can
        # push broadband static past what webrtcvad rejects; files containing
        # real speech keep their noise at natural relative level.
        if noise_filter and not has_speech(chunk):
            log(
                f"BATCH Non-speech segment skipped (noise filter): "
                f"{start_s:.1f}s–{end_s:.1f}s",
                level="INFO",
            )
            if progress_callback is not None:
                progress_callback(i + 1, total)
            continue

        wav_bytes = _segment_to_wav_bytes(chunk)
        transcription = transcribe_with_fallback(
            stt_provider,
            models_to_try,
            wav_bytes,
            lang_code,
            prompt=prev_tail or None,
            log_prefix="BATCH ",
        )
        if (
            transcription is not None
            and len(prev_tail) >= _PROMPT_ECHO_MIN_CHARS
            and transcription.strip() == prev_tail
        ):
            log(
                "BATCH Dropped verbatim prompt echo (non-speech segment)",
                level="WARNING",
            )
            transcription = None
        if transcription is None or not transcription.strip():
            if progress_callback is not None:
                progress_callback(i + 1, total)
            continue
        prev_tail = transcription.strip()[-_PROMPT_TAIL_CHARS:]

        # Mirror the live pipeline: a secondary Arabic transcription feeds
        # the Quran/Athan matchers (skip conditions in translation/stt).
        arabic_transcription = maybe_arabic_retranscription(
            stt_provider,
            models_to_try[0],
            wav_bytes,
            transcription=transcription,
            source_lang_code=lang_code,
            source_language=src_language,
            target_language=tgt_language,
            islamic_mode=load_settings().islamic_mode,
            log_prefix="BATCH ",
        )

        context = "\n".join(recent)
        translation = translate_text(
            transcription,
            context,
            arabic_text=arabic_transcription,
            model=translation_model,
            provider=translation_provider,
            source_language=src_language,
            target_language=tgt_language,
        )
        recent.append(transcription)
        del recent[:-CONTEXT_RECENT_RAW_COUNT]

        # An empty translation means GPT judged the segment unintelligible
        # (system-prompt rule) — no SRT line, no transcript entry, no
        # "unverständlich"-style meta-comment on screen.
        if translation.strip():
            entries.append(
                SrtEntry(
                    start=start_s,
                    end=end_s,
                    text=translation,
                    source=transcription if bilingual_srt else None,
                )
            )
            records.append((start_s, transcription, translation))
        if progress_callback is not None:
            progress_callback(i + 1, total)

    out_path: str | None = None
    if output_format in ("srt", "both"):
        out_path = output_path_for(input_path, tgt_language)
        write_srt(entries, out_path)
        log(f"BATCH Wrote {len(entries)} subtitles to {out_path}", level="INFO")
    if output_format in ("txt", "both"):
        txt_path = output_path_for(input_path, tgt_language, ext="txt")
        write_text(records, txt_path, src_language, tgt_language)
        log(f"BATCH Wrote transcript to {txt_path}", level="INFO")
        out_path = out_path or txt_path

    # In-app record (browsable/removable/summarizable in the history viewer).
    # Never let a persistence hiccup fail the SRT export the user asked for.
    # ``formats`` records what the user exported (drives the history badges);
    # an exact SRT copy is kept next to the record so it survives in-app even
    # after the source file (and its SRT) is moved or deleted.
    record_path = write_batch_record(
        os.path.basename(input_path),
        src_language,
        tgt_language,
        records,
        formats=output_format,
    )
    if record_path and output_format in ("srt", "both"):
        try:
            write_srt(entries, batch_srt_path(record_path))
        except OSError as e:
            log(f"BATCH SRT sidecar write failed: {e}", level="WARNING")
    return out_path
