"""Thread-safe input-level metering for live and preview capture.

The meter accepts normalized floating-point audio as well as integer PCM.
Signal statistics are calculated outside the state lock so an audio callback
only holds the lock long enough to publish its latest values.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

DBFS_FLOOR = -96.0


def _to_dbfs(value: float) -> float:
    """Convert a normalized linear amplitude to dBFS with a useful floor."""

    floor_amplitude = 10.0 ** (DBFS_FLOOR / 20.0)
    if value <= floor_amplitude:
        return DBFS_FLOOR
    return 20.0 * math.log10(value)


@dataclass(frozen=True, slots=True)
class AudioLevelSnapshot:
    """One immutable view of the most recently captured mono PCM block.

    ``rms`` is attack/release smoothed for a readable UI. ``peak`` and
    ``clipping_ratio`` describe the latest block, while ``peak_hold`` retains
    brief transients. Linear values use full scale = 1.0; their dBFS
    counterparts use :data:`DBFS_FLOOR` for digital silence.
    """

    rms: float
    peak: float
    peak_hold: float
    rms_dbfs: float
    peak_dbfs: float
    peak_hold_dbfs: float
    clipping_ratio: float
    updated_at: float
    is_stale: bool

    @property
    def clipped(self) -> bool:
        """Whether the latest input block contains near-full-scale samples."""

        return self.clipping_ratio > 0.0


class AudioLevelMeter:
    """Publish smoothed RMS, peak hold, and clipping for PCM input blocks.

    The defaults favour a responsive attack, a calmer release, and a short
    peak hold. If capture stops, :meth:`snapshot` returns a reset/stale value
    rather than leaving a misleading level frozen on screen.
    """

    def __init__(
        self,
        *,
        attack_seconds: float = 0.045,
        release_seconds: float = 0.30,
        peak_hold_seconds: float = 0.65,
        peak_decay_db_per_second: float = 24.0,
        stale_after_seconds: float = 0.75,
        clipping_threshold: float = 0.999,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if attack_seconds <= 0 or release_seconds <= 0:
            raise ValueError("attack and release times must be positive")
        if peak_hold_seconds < 0 or peak_decay_db_per_second <= 0:
            raise ValueError("peak hold settings must be non-negative")
        if stale_after_seconds <= 0:
            raise ValueError("stale timeout must be positive")
        if not 0 < clipping_threshold <= 1:
            raise ValueError("clipping threshold must be in (0, 1]")

        self._attack_seconds = attack_seconds
        self._release_seconds = release_seconds
        self._peak_hold_seconds = peak_hold_seconds
        self._peak_decay_db_per_second = peak_decay_db_per_second
        self._stale_after_seconds = stale_after_seconds
        self._clipping_threshold = clipping_threshold
        self._clock = clock
        self._lock = threading.Lock()

        self._smoothed_rms = 0.0
        self._latest_peak = 0.0
        self._peak_hold = 0.0
        self._clipping_ratio = 0.0
        self._last_update: float | None = None
        self._peak_hold_until = 0.0
        self._peak_decay_at = 0.0

    @staticmethod
    def _normalize(samples: np.ndarray) -> np.ndarray:
        """Return a flat floating-point array whose full scale is 1.0."""

        array = np.asarray(samples)
        if array.size == 0:
            return np.empty(0, dtype=np.float32)

        if np.issubdtype(array.dtype, np.signedinteger):
            info = np.iinfo(array.dtype)
            full_scale = float(max(abs(info.min), info.max))
            normalized = array.astype(np.float32) / full_scale
        elif np.issubdtype(array.dtype, np.unsignedinteger):
            info = np.iinfo(array.dtype)
            midpoint = (float(info.max) + 1.0) / 2.0
            normalized = (array.astype(np.float32) - midpoint) / midpoint
        elif np.issubdtype(array.dtype, np.floating):
            normalized = array.astype(np.float32, copy=False)
        else:
            raise TypeError(f"Unsupported PCM dtype: {array.dtype}")

        normalized = normalized.reshape(-1)
        if not np.isfinite(normalized).all():
            normalized = np.nan_to_num(normalized, copy=True)
        return normalized

    def _decay_peak_locked(self, now: float) -> None:
        if now <= self._peak_hold_until:
            self._peak_decay_at = self._peak_hold_until
            return

        decay_start = max(self._peak_decay_at, self._peak_hold_until)
        elapsed = max(0.0, now - decay_start)
        if elapsed:
            decay_db = self._peak_decay_db_per_second * elapsed
            self._peak_hold *= 10.0 ** (-decay_db / 20.0)
        self._peak_decay_at = now

    def _reset_locked(self) -> None:
        self._smoothed_rms = 0.0
        self._latest_peak = 0.0
        self._peak_hold = 0.0
        self._clipping_ratio = 0.0
        self._last_update = None
        self._peak_hold_until = 0.0
        self._peak_decay_at = 0.0

    def reset(self) -> None:
        """Immediately clear all published level state."""

        with self._lock:
            self._reset_locked()

    def observe(
        self,
        samples: np.ndarray,
        *,
        sample_rate: int | None = None,
    ) -> None:
        """Measure and publish one mono PCM block.

        ``sample_rate`` lets smoothing advance by at least the block duration
        when callbacks arrive back-to-back under the same clock tick.
        """

        normalized = self._normalize(samples)
        if normalized.size == 0:
            return

        absolute = np.abs(normalized)
        peak = float(np.max(absolute))
        rms = float(np.sqrt(np.mean(normalized * normalized, dtype=np.float64)))
        clipping_ratio = float(np.count_nonzero(absolute >= self._clipping_threshold))
        clipping_ratio /= float(normalized.size)
        now = self._clock()

        with self._lock:
            if self._last_update is None:
                smoothed_rms = rms
            else:
                elapsed = max(0.0, now - self._last_update)
                if sample_rate is not None and sample_rate > 0:
                    elapsed = max(elapsed, normalized.size / float(sample_rate))
                time_constant = (
                    self._attack_seconds
                    if rms >= self._smoothed_rms
                    else self._release_seconds
                )
                weight = 1.0 - math.exp(-elapsed / time_constant)
                smoothed_rms = self._smoothed_rms + weight * (
                    rms - self._smoothed_rms
                )

            self._decay_peak_locked(now)
            if peak >= self._peak_hold:
                self._peak_hold = peak
                self._peak_hold_until = now + self._peak_hold_seconds
                self._peak_decay_at = self._peak_hold_until

            self._smoothed_rms = smoothed_rms
            self._latest_peak = peak
            self._clipping_ratio = clipping_ratio
            self._last_update = now

    def snapshot(self) -> AudioLevelSnapshot:
        """Return the latest immutable values, resetting after capture stalls."""

        now = self._clock()
        with self._lock:
            if (
                self._last_update is None
                or now - self._last_update >= self._stale_after_seconds
            ):
                self._reset_locked()
                return AudioLevelSnapshot(
                    rms=0.0,
                    peak=0.0,
                    peak_hold=0.0,
                    rms_dbfs=DBFS_FLOOR,
                    peak_dbfs=DBFS_FLOOR,
                    peak_hold_dbfs=DBFS_FLOOR,
                    clipping_ratio=0.0,
                    updated_at=0.0,
                    is_stale=True,
                )

            self._decay_peak_locked(now)
            return AudioLevelSnapshot(
                rms=self._smoothed_rms,
                peak=self._latest_peak,
                peak_hold=self._peak_hold,
                rms_dbfs=_to_dbfs(self._smoothed_rms),
                peak_dbfs=_to_dbfs(self._latest_peak),
                peak_hold_dbfs=_to_dbfs(self._peak_hold),
                clipping_ratio=self._clipping_ratio,
                updated_at=self._last_update,
                is_stale=False,
            )
