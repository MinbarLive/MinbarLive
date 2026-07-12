import re
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass

from config import SEMANTIC_MAX_CHUNKS, SEMANTIC_MAX_SECONDS, SEMANTIC_MAX_WORDS
from utils.logging import log

_WHITESPACE_RE = re.compile(r"\s+")
# Split a flushed buffer into subtitles at sentence ends only — never mid
# sentence, so no fragment is ever handed to the translator.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.؟!…])\s+")


def _split_for_display(text: str, max_words: int = SEMANTIC_MAX_WORDS) -> list[str]:
    """Break an over-long flush into subtitle-sized pieces at sentence ends.

    A dense flush can carry several sentences (~40+ words) that scroll away
    before a viewer can read them. Whole sentences are greedily packed up to
    ``max_words``; a single sentence longer than the cap is kept intact (its
    own piece) rather than fragmented, so translation quality never suffers.
    Returns [text] unchanged when it already fits or has no sentence break.
    """
    if len(text.split()) <= max_words:
        return [text]
    sentences = [s for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    if len(sentences) < 2:
        return [text]  # one long run-on sentence — don't fragment it
    chunks: list[str] = []
    current: list[str] = []
    current_words = 0
    for sentence in sentences:
        words = len(sentence.split())
        if current and current_words + words > max_words:
            chunks.append(" ".join(current))
            current, current_words = [], 0
        current.append(sentence)
        current_words += words
    if current:
        chunks.append(" ".join(current))
    return chunks or [text]


@dataclass
class AudioSegment:
    file_path: str
    transcription: str
    is_silent: bool
    timestamp: float


class ProcessingStrategy(ABC):

    @abstractmethod
    def add_segment(self, segment: AudioSegment) -> list[str]:
        pass

    @abstractmethod
    def flush(self) -> list[str]:
        pass

    def flush_if_stale(self) -> list[str]:
        """Flush only if buffered text has outlived the strategy's timeout.

        Called periodically by the controller: during silence no segments
        arrive, so a timeout that only runs inside add_segment could never
        fire. Strategies without a buffer keep this default no-op.
        """
        return []

    @abstractmethod
    def reset(self):
        pass


class ChunkBasedStrategy(ProcessingStrategy):

    def __init__(self):
        self.reset()

    def add_segment(self, segment: AudioSegment) -> list[str]:
        if segment.is_silent:
            return []
        return [segment.transcription] if segment.transcription.strip() else []

    def flush(self) -> list[str]:
        return []

    def reset(self):
        pass


class SemanticBufferingStrategy(ProcessingStrategy):

    def __init__(
        self,
        max_chunks: int = SEMANTIC_MAX_CHUNKS,
        max_seconds: float = SEMANTIC_MAX_SECONDS,
    ):
        self.max_chunks = max_chunks
        self.max_seconds = max_seconds
        self.reset()

    def reset(self):
        self.buffer = deque()
        self.start_time = None
        log("Semantic buffer reset", level="DEBUG")

    def _looks_semantically_complete(self, text: str) -> bool:

        text = text.strip()
        if not text:
            return False

        # Check for sentence endings (Arabic punctuation)
        if text.endswith(("؟", ".", "!", "…")):
            return True

        # Check for minimum word count
        word_count = len(text.split())
        return word_count >= 18

    def _buffer_text(self) -> str:
        """Non-silent transcriptions joined into one whitespace-clean string.

        Joining with a single space (and collapsing any newlines/repeated
        spaces the STT left inside a segment) keeps two buffered pieces from
        fusing into one word on the rendered source line.
        """
        joined = " ".join(
            s.transcription
            for s in self.buffer
            if not s.is_silent and s.transcription.strip()
        )
        return _WHITESPACE_RE.sub(" ", joined).strip()

    def _should_flush(self) -> bool:
        if not self.buffer:
            return False

        # Timeout check
        if (
            self.start_time is not None
            and (time.time() - self.start_time) > self.max_seconds
        ):
            log(f"Semantic buffer timeout after {self.max_seconds}s", level="DEBUG")
            return True

        # Count pending non-silent segments
        pending = [
            s for s in self.buffer if not s.is_silent and s.transcription.strip()
        ]
        if len(pending) >= self.max_chunks:
            log(f"Semantic buffer max chunks reached: {len(pending)}", level="DEBUG")
            return True

        # Check semantic completeness
        if self._looks_semantically_complete(self._buffer_text()):
            log("Semantic buffer: text looks complete", level="DEBUG")
            return True

        return False

    def add_segment(self, segment: AudioSegment) -> list[str]:
        self.buffer.append(segment)

        if self.start_time is None:
            self.start_time = segment.timestamp

        if self._should_flush():
            return self._flush_buffer()

        return []

    def _flush_buffer(self) -> list[str]:
        if not self.buffer:
            return []

        buffer_text = self._buffer_text()

        if not buffer_text:
            # No text to translate — still reset so stale segments don't
            # retrigger the flush conditions forever
            self.buffer.clear()
            self.start_time = None
            return []

        # Clear the entire buffer after flush - no overlap to avoid repetition
        self.buffer.clear()
        self.start_time = None

        return _split_for_display(buffer_text)

    def flush(self) -> list[str]:
        return self._flush_buffer()

    def flush_if_stale(self) -> list[str]:
        if not self.buffer or self.start_time is None:
            return []
        if (time.time() - self.start_time) > self.max_seconds:
            log(
                f"Semantic buffer timeout after {self.max_seconds}s (idle flush)",
                level="DEBUG",
            )
            return self._flush_buffer()
        return []
