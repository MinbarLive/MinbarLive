"""Audio capture with ring buffer and silence detection.

Note: This module uses module-level global state for the ring buffer.
This is intentional for performance in the audio callback (which must be
as fast as possible).
"""

from __future__ import annotations

import threading
from typing import Optional

import numpy as np
import sounddevice as sd

from config import FS, RING_CAPACITY, DURATION, SILENCE_THRESHOLD, SILENCE_RATIO
from utils.logging import log

# Ring buffer for audio capture (global for performance in callback)
_ring = np.zeros(RING_CAPACITY, dtype=np.float32)
_ring_write_idx = 0
_ring_lock = threading.Lock()


def reset_ring_buffer():
    """Reset the ring buffer to its initial state. Call before starting a new session."""
    global _ring, _ring_write_idx
    with _ring_lock:
        _ring[:] = 0
        _ring_write_idx = 0


def audio_callback(indata, frames, time_info, status):
    """
    Sounddevice callback that writes incoming audio to the ring buffer.
    """
    global _ring_write_idx

    if status:
        log(f"AUDIO-CALLBACK Status: {status}", level="DEBUG")

    chunk = indata[:, 0].astype(np.float32)
    n = chunk.shape[0]

    with _ring_lock:
        end = (_ring_write_idx + n) % RING_CAPACITY
        if end > _ring_write_idx:
            _ring[_ring_write_idx:end] = chunk
        else:
            part = RING_CAPACITY - _ring_write_idx
            _ring[_ring_write_idx:] = chunk[:part]
            _ring[:end] = chunk[part:]
        _ring_write_idx = end


def get_ring_segment(duration_seconds: Optional[float] = None) -> np.ndarray:
    """
    Extract a segment from the ring buffer.

    Args:
        duration_seconds: Length of segment to extract. Defaults to DURATION.

    Returns:
        Audio segment as float32 numpy array.
    """
    if duration_seconds is None:
        duration_seconds = DURATION

    needed = int(duration_seconds * FS)

    with _ring_lock:
        write_idx = _ring_write_idx
        start = (write_idx - needed) % RING_CAPACITY
        if start < write_idx:
            segment = _ring[start:write_idx].copy()
        else:
            segment = np.concatenate((_ring[start:], _ring[:write_idx]))

    return segment


def get_default_input_device() -> int:
    """
    Get the system default input device.

    Returns:
        Device index for sounddevice.

    Raises:
        RuntimeError: If no input devices are available.
    """
    try:
        default = sd.default.device[0]  # Input device
        if default is not None and default >= 0:
            devices = sd.query_devices()
            if default < len(devices) and devices[default]["max_input_channels"] > 0:
                log(f"Using default input device: {devices[default]['name']}")
                return default
    except Exception as e:
        log(f"Error getting default input device: {e}", level="DEBUG")

    # Fallback: find first available input device
    devices = sd.query_devices()
    for i, d in enumerate(devices):
        if d["max_input_channels"] > 0:
            log(f"Fallback to first input device: {d['name']}")
            return i

    raise RuntimeError("No input devices available.")


def is_silence(
    audio_data: np.ndarray,
    threshold: Optional[float] = None,
    silence_ratio: Optional[float] = None,
) -> bool:
    """
    Detect if audio data is predominantly silence.

    Args:
        audio_data: Audio samples as numpy array.
        threshold: RMS threshold below which a frame is considered silent.
        silence_ratio: Fraction of frames that must be silent.

    Returns:
        True if audio is mostly silence.
    """
    if threshold is None:
        threshold = SILENCE_THRESHOLD
    if silence_ratio is None:
        silence_ratio = SILENCE_RATIO

    if audio_data.size == 0:
        return True

    # Compute RMS in windows (50ms frames)
    window_len = max(1, int(50 * FS / 1000))
    n_frames = audio_data.size // window_len

    if n_frames == 0:
        rms = np.sqrt(np.mean(audio_data**2))
        return rms < threshold

    frames = audio_data[: n_frames * window_len].reshape(n_frames, window_len)
    rms_vals = np.sqrt(np.mean(frames**2, axis=1))
    silent_frames = np.sum(rms_vals < threshold)
    ratio = silent_frames / float(len(rms_vals))

    return ratio >= silence_ratio
