"""
Audio segment writing and file I/O.
"""

from __future__ import annotations

import os
import time
import queue
import uuid

import numpy as np
import scipy.io.wavfile as wavfile
from datetime import datetime

from config import AUDIO_DIR, FS, DURATION, STEP, SAME_LANG_DURATION, SAME_LANG_STEP
from utils.logging import log
from utils.settings import load_settings
from audio.capture import get_ring_segment, is_silence

# Queue for async file writing
write_queue = queue.Queue()


def clear_write_queue():
    """Clear any pending items in the write queue. Call before starting a new session."""
    cleared = 0
    while not write_queue.empty():
        try:
            write_queue.get_nowait()
            write_queue.task_done()
            cleared += 1
        except queue.Empty:
            break
    if cleared > 0:
        log(f"Cleared {cleared} pending items from write queue", level="DEBUG")


def segment_writer(stop_event) -> None:
    """
    Periodically extracts audio segments from the ring buffer and queues them for writing.
    Skips segments that are predominantly silence.

    Uses shorter segments with less overlap for same-language mode (faster feedback).
    """
    segments_checked = 0

    while not stop_event.is_set():
        # load_settings() serves the in-memory cache — no disk read per loop
        settings = load_settings()
        same_language = settings.source_language == settings.target_language

        # Use shorter segments for same-language mode
        duration = SAME_LANG_DURATION if same_language else DURATION
        step = SAME_LANG_STEP if same_language else STEP

        segment = get_ring_segment(duration)

        if segment.size < int(duration * FS):
            time.sleep(0.1)
            continue

        arr = segment.astype(np.float32)

        if is_silence(arr):
            log(
                f"SegmentWriter: silence detected, skipping segment #{segments_checked}",
                level="DEBUG",
            )
        else:
            ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
            uid = uuid.uuid4().hex[:8]
            filename = os.path.join(AUDIO_DIR, f"recording_{ts}_{uid}.wav")

            try:
                write_queue.put_nowait((filename, np.int16(arr * 32767)))
                log(f"SegmentWriter: queued {filename}")
            except queue.Full:
                log(f"SegmentWriter: write_queue full, drop {filename}", level="ERROR")

        segments_checked += 1
        time.sleep(step)  # Use same-language step if applicable


def async_write_audio(stop_event) -> None:
    """
    Consumer thread that writes audio files from the queue to disk.
    Uses atomic write (temp file + rename) for safety.
    """
    while not stop_event.is_set():
        try:
            try:
                fn, audio_int16 = write_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            tmp = fn + ".tmp"
            try:
                wavfile.write(tmp, FS, audio_int16)
                os.replace(tmp, fn)
                log(f"AudioWriter: wrote {fn}", level="DEBUG")
            except Exception as e:
                log(f"AudioWriter write error for {fn}: {e}", level="ERROR")
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except Exception as cleanup_err:
                    log(
                        f"Failed to cleanup temp file {tmp}: {cleanup_err}",
                        level="DEBUG",
                    )
            finally:
                write_queue.task_done()
        except Exception as e:
            log(f"AudioWriter outer error: {e}", level="ERROR")

        time.sleep(0.01)
