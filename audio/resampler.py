"""Streaming sample-rate conversion for the capture path.

Some inputs cannot be opened at the pipeline rate ``FS``.  On Linux/PipeWire
every named source (Bluetooth headsets, internal mics) is exposed through the
JACK host API, which offers only the device's native rate (typically 48 kHz)
and does no rate conversion of its own — the generic ``pulse``/``default`` ALSA
aliases resample, but a specific device does not.  So the capture loop opens
such a device at its native rate and feeds the blocks through a
:class:`StreamResampler` to reach ``FS``.

The resampler is a stateful windowed-sinc (polyphase-style) converter: it keeps
enough trailing input between calls that consecutive blocks join without the
clicks a per-block stateless resample would produce.  numpy only — the packaged
build deliberately omits ``scipy.signal`` (see MinbarLive.spec).
"""

from __future__ import annotations

import math

import numpy as np


class StreamResampler:
    """Convert a stream of audio blocks from ``src_rate`` to ``dst_rate``.

    ``process`` accepts and returns 1-D mono or 2-D ``(frames, channels)``
    arrays and preserves the input dtype (int16 stays int16).  When the two
    rates are equal it is a zero-copy pass-through.
    """

    def __init__(
        self,
        src_rate: int,
        dst_rate: int,
        channels: int = 1,
        *,
        half_width: int = 16,
    ) -> None:
        if src_rate <= 0 or dst_rate <= 0:
            raise ValueError("sample rates must be positive")
        self.src_rate = int(src_rate)
        self.dst_rate = int(dst_rate)
        self.channels = int(channels)
        self.passthrough = self.src_rate == self.dst_rate

        # Anti-alias cutoff relative to the *input* Nyquist: 1.0 when
        # upsampling, dst/src when downsampling (so we band-limit to the lower
        # output Nyquist before decimating).
        self._cutoff = min(1.0, self.dst_rate / self.src_rate)
        # Input samples advanced per output sample.
        self._step = self.src_rate / self.dst_rate
        # Kernel half-width in input samples; wider when downsampling because
        # the sinc is stretched by 1/cutoff.
        self._radius = max(1, int(math.ceil(half_width / self._cutoff)))
        self._taps = np.arange(-self._radius + 1, self._radius + 1)

        # History primed with a kernel radius of silence so the first real
        # sample can sit at the centre of the kernel (a few samples of edge
        # ramp, not a dropped head).
        self._hist = np.zeros((self._radius, self.channels), dtype=np.float64)
        self._phase = float(self._radius)

    def reset(self) -> None:
        """Drop carried-over state (call before reusing across sessions)."""
        self._hist = np.zeros((self._radius, self.channels), dtype=np.float64)
        self._phase = float(self._radius)

    def process(self, block: np.ndarray) -> np.ndarray:
        """Resample one block, carrying filter state to the next call."""
        if self.passthrough:
            return block

        mono_in = block.ndim == 1
        out_dtype = block.dtype
        x = np.asarray(block, dtype=np.float64)
        if mono_in:
            x = x[:, None]

        buf = np.concatenate([self._hist, x], axis=0)
        n = buf.shape[0]
        radius = self._radius
        step = self._step
        last_center = n - 1 - radius  # need `radius` future samples per output

        if last_center < self._phase:
            # Not enough new samples yet to emit an output — just accumulate.
            self._hist = buf
            empty = np.zeros((0,) if mono_in else (0, self.channels), out_dtype)
            return empty

        count = int(math.floor((last_center - self._phase) / step)) + 1
        positions = self._phase + step * np.arange(count)  # (count,)
        centers = np.floor(positions).astype(np.int64)  # (count,)
        idx = centers[:, None] + self._taps[None, :]  # (count, 2*radius)
        dist = positions[:, None] - idx  # fractional distance to each tap

        weights = self._cutoff * np.sinc(self._cutoff * dist) * _hann(dist / radius)
        # Normalise per output so the kernel has unity DC gain regardless of
        # the fractional phase — keeps levels exact.
        weights /= weights.sum(axis=1, keepdims=True)

        gathered = buf[idx]  # (count, 2*radius, channels)
        out = np.einsum("ct,ctk->ck", weights, gathered)  # (count, channels)

        # Retain the tail the next block's first output still needs.
        next_phase = self._phase + step * count
        keep_from = int(math.floor(next_phase)) - radius + 1
        keep_from = max(0, min(keep_from, n))
        self._hist = buf[keep_from:]
        self._phase = next_phase - keep_from

        if np.issubdtype(out_dtype, np.integer):
            info = np.iinfo(out_dtype)
            out = np.clip(np.rint(out), info.min, info.max)
        out = out.astype(out_dtype)
        return out[:, 0] if mono_in else out


def _hann(t: np.ndarray) -> np.ndarray:
    """Hann window over ``|t| < 1``; zero outside so the kernel tapers off."""
    return np.where(np.abs(t) < 1.0, 0.5 + 0.5 * np.cos(np.pi * t), 0.0)
