"""
Adaptive context management with async summarization.

Provides efficient context for translation without adding latency:
- Recent raw transcriptions (3 segments) for immediate disambiguation
- Rolling summary updated every ~10 segments (async)
- Hourly summaries for long-term context (async)

Token budget target: ~1000-1500 tokens total context.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field

from config import (
    CONTEXT_HOURLY_INTERVAL,
    CONTEXT_RECENT_RAW_COUNT,
    CONTEXT_SUMMARIZE_EVERY_N,
    CONTEXT_SUMMARIZE_MIN_SECONDS,
)
from providers import get_translation_model_chain, get_translation_provider
from utils.logging import log
from utils.settings import load_settings

# Output budgets for the summary calls. On reasoning models (gpt-5.x) the
# hidden reasoning tokens count against max_completion_tokens too (same
# lesson as utils/session_summary), so these are far above the visible
# ~50/~20-word targets — a tight cap gets consumed by reasoning and the
# summary comes back truncated or empty.
_ROLLING_SUMMARY_MAX_TOKENS = 1000
_HOURLY_SUMMARY_MAX_TOKENS = 500


def _get_translation_model() -> str:
    """Primary translation model of the active provider."""
    return get_translation_model_chain()[0]


@dataclass
class ContextState:
    """Holds all context state for a session."""

    recent_raw: deque = field(
        default_factory=lambda: deque(maxlen=CONTEXT_RECENT_RAW_COUNT)
    )
    pending_for_summary: list = field(default_factory=list)
    rolling_summary: str = ""
    hourly_summaries: list = field(default_factory=list)
    transcription_count: int = 0
    session_start: float = field(default_factory=time.time)
    last_hourly_summary_time: float = field(default_factory=time.time)
    # Starts at session start: the first rolling summary waits out the time
    # floor too (the first minutes are covered by the raw recent segments).
    last_rolling_summary_time: float = field(default_factory=time.time)


class ContextManager:
    """
    Manages translation context with async summarization.

    Usage:
        ctx_mgr = ContextManager()
        ctx_mgr.start()  # Start background summarization thread

        # After each transcription:
        ctx_mgr.add_transcription(text)
        context = ctx_mgr.get_context()  # Use for translation

        ctx_mgr.stop()  # On shutdown
    """

    def __init__(self):
        self._state = ContextState()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._summarize_event = threading.Event()  # Signals pending summarization work
        self._thread: threading.Thread | None = None

    def start(self):
        """Start the background summarization thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._summarization_loop, daemon=True, name="context-summarizer"
        )
        self._thread.start()
        log("ContextManager started", level="INFO")

    def stop(self, timeout: float = 2.0):
        """Stop the background thread."""
        self._stop_event.set()
        self._summarize_event.set()  # Wake up thread if waiting
        if self._thread:
            self._thread.join(timeout=timeout)
            self._thread = None
        log("ContextManager stopped", level="INFO")

    def reset(self):
        """Reset all context (for new session)."""
        with self._lock:
            self._state = ContextState()
        log("ContextManager reset", level="DEBUG")

    def add_transcription(self, text: str, enable_summarization: bool = True):
        """
        Add a new transcription. Updates recent raw and queues for summarization.
        This is fast - summarization happens async.

        Args:
            text: The transcription text to add.
            enable_summarization: If False, skip summarization (useful for same-language
                mode where context is not needed for translation).
        """
        text = (text or "").strip()
        if not text:
            return

        with self._lock:
            # Always keep in recent raw
            self._state.recent_raw.append(text)
            self._state.transcription_count += 1

            # Skip summarization if disabled (same-language mode)
            if not enable_summarization:
                return

            # Queue for summarization
            self._state.pending_for_summary.append(text)

            # Trigger summarization on BOTH enough pending texts AND enough
            # time since the last summary. The count alone made streaming
            # mode (utterances every ~3-8s) refresh a near-identical summary
            # every ~45-90s — one wasted LLM call each time.
            now = time.time()
            should_summarize = (
                len(self._state.pending_for_summary) >= CONTEXT_SUMMARIZE_EVERY_N
                and now - self._state.last_rolling_summary_time
                >= CONTEXT_SUMMARIZE_MIN_SECONDS
            )

            # Check for hourly summary
            should_hourly = (
                now - self._state.last_hourly_summary_time >= CONTEXT_HOURLY_INTERVAL
                and self._state.rolling_summary  # Only if we have something to summarize
            )

        if should_summarize or should_hourly:
            self._summarize_event.set()  # Wake up background thread

    def get_context(self) -> str:
        """
        Get the current context string for translation.
        Combines: hourly summaries + rolling summary + recent raw.

        Returns immediately with current state (no blocking).
        """
        with self._lock:
            parts = []

            # 1. Hourly summaries (oldest first)
            if self._state.hourly_summaries:
                hours_context = " | ".join(self._state.hourly_summaries)
                parts.append(f"[Session overview: {hours_context}]")

            # 2. Rolling summary of recent content
            if self._state.rolling_summary:
                parts.append(f"[Recent topics: {self._state.rolling_summary}]")

            # 3. Recent raw transcriptions (for immediate disambiguation)
            if self._state.recent_raw:
                recent = "\n".join(self._state.recent_raw)
                parts.append(f"[Last segments:\n{recent}]")

            return "\n\n".join(parts)

    def get_stats(self) -> dict:
        """Get current context statistics."""
        with self._lock:
            elapsed = time.time() - self._state.session_start
            return {
                "transcription_count": self._state.transcription_count,
                "session_minutes": round(elapsed / 60, 1),
                "hourly_summaries": len(self._state.hourly_summaries),
                "pending_for_summary": len(self._state.pending_for_summary),
                "has_rolling_summary": bool(self._state.rolling_summary),
            }

    def _summarization_loop(self):
        """Background thread that handles async summarization."""
        log("Summarization loop started", level="DEBUG")

        while not self._stop_event.is_set():
            # Wait for work or stop signal
            self._summarize_event.wait(timeout=30.0)
            self._summarize_event.clear()

            if self._stop_event.is_set():
                break

            try:
                self._do_summarization_work()
            except Exception as e:
                log(f"Summarization error: {e}", level="ERROR")

        log("Summarization loop ended", level="DEBUG")

    def _do_summarization_work(self):
        """Perform pending summarization tasks."""
        with self._lock:
            pending = self._state.pending_for_summary.copy()
            current_rolling = self._state.rolling_summary
            now = time.time()
            rolling_due = (
                now - self._state.last_rolling_summary_time
                >= CONTEXT_SUMMARIZE_MIN_SECONDS
            )
            needs_hourly = (
                now - self._state.last_hourly_summary_time >= CONTEXT_HOURLY_INTERVAL
                and current_rolling
            )

        # 1. Update rolling summary if we have enough pending AND the time
        #    floor has passed (same dual condition as add_transcription).
        if len(pending) >= CONTEXT_SUMMARIZE_EVERY_N and rolling_due:
            segment_count = self._state.transcription_count
            start_seg = segment_count - len(pending) + 1
            end_seg = segment_count
            log(
                f"CONTEXT Rolling summary starting: segments {start_seg}-{end_seg} ({len(pending)} total)",
                level="INFO",
            )
            # Log first and last segment for context
            if pending:
                log(f"CONTEXT First segment: {pending[0][:80]}...", level="DEBUG")
                log(f"CONTEXT Last segment: {pending[-1][:80]}...", level="DEBUG")

            new_rolling = self._create_rolling_summary(pending, current_rolling)
            with self._lock:
                self._state.rolling_summary = new_rolling
                # Remove exactly what was summarized — texts that arrived
                # DURING the API call must survive for the next summary
                # (clear() silently dropped them; visible in streaming mode,
                # where utterances land every few seconds).
                del self._state.pending_for_summary[: len(pending)]
                self._state.last_rolling_summary_time = time.time()
            log(
                f"CONTEXT Rolling summary complete: {new_rolling}",
                level="INFO",
            )

        # 2. Create hourly summary if needed
        if needs_hourly:
            with self._lock:
                rolling_to_archive = self._state.rolling_summary
                hour_num = len(self._state.hourly_summaries) + 1

            log(
                f"CONTEXT Hourly summary #{hour_num} starting. Rolling to archive: {rolling_to_archive}",
                level="INFO",
            )
            hourly = self._create_hourly_summary(rolling_to_archive, hour_num)

            with self._lock:
                self._state.hourly_summaries.append(hourly)
                self._state.last_hourly_summary_time = now
                # Keep rolling summary but note it's been archived

            log(f"CONTEXT Hourly summary #{hour_num} complete: {hourly}", level="INFO")

    def _create_rolling_summary(
        self, transcriptions: list, previous_summary: str
    ) -> str:
        """Summarize recent transcriptions into a rolling summary."""
        if not transcriptions:
            return previous_summary

        text_block = "\n".join(transcriptions)

        # Match the translation prompt: Islamic framing only in Islamic mode.
        # Read fresh so a mid-session toggle applies to the next summary.
        if load_settings().islamic_mode:
            intro = (
                "Summarize the key topics and themes from this mosque "
                "sermon/khutbah transcript."
            )
            focus = (
                "Focus on: main religious topics discussed, any Quran verses "
                "mentioned, key Islamic concepts."
            )
        else:
            intro = "Summarize the key topics and themes from this transcript."
            focus = "Focus on the main topics and points discussed."

        prompt = f"""{intro}
Be extremely concise (max 2-3 sentences, under 50 words).
{focus}
Do NOT include greetings, filler, or repetition.

{"Previous context: " + previous_summary if previous_summary else ""}

Recent transcriptions:
{text_block}

Concise summary:"""

        try:
            result = get_translation_provider().complete(
                model=_get_translation_model(),
                user_prompt=prompt,
                max_output_tokens=_ROLLING_SUMMARY_MAX_TOKENS,
                temperature=0.3,
            )
            if not result:
                # A truncated/empty response must not wipe the summary.
                log("Rolling summary came back empty, keeping old", level="WARNING")
                return previous_summary
            return result
        except Exception as e:
            log(f"Rolling summary API error: {e}", level="ERROR")
            return previous_summary  # Keep old summary on failure

    def _create_hourly_summary(self, rolling_summary: str, hour_num: int) -> str:
        """Compress rolling summary into a very short hourly summary."""
        if not rolling_summary:
            return f"Hour {hour_num}: (no content)"

        subject = "sermon summary" if load_settings().islamic_mode else "summary"
        prompt = f"""Compress this {subject} into ONE short sentence (max 20 words).
Keep only the most important topic or theme.

Summary to compress:
{rolling_summary}

One-sentence summary for hour {hour_num}:"""

        try:
            result = get_translation_provider().complete(
                model=_get_translation_model(),
                user_prompt=prompt,
                max_output_tokens=_HOURLY_SUMMARY_MAX_TOKENS,
                temperature=0.2,
            )
            if not result:
                return f"Hr{hour_num}: {rolling_summary[:50]}..."
            return f"Hr{hour_num}: {result}"
        except Exception as e:
            log(f"Hourly summary API error: {e}", level="ERROR")
            return f"Hr{hour_num}: {rolling_summary[:50]}..."  # Fallback: truncate


# Singleton instance for app-wide use
_context_manager: ContextManager | None = None


def get_context_manager() -> ContextManager:
    """Get or create the singleton ContextManager."""
    global _context_manager
    if _context_manager is None:
        _context_manager = ContextManager()
    return _context_manager
