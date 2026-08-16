"""Provider-agnostic interfaces for AI capabilities.

Pipeline code (translator, rag, app_controller, context_manager) must talk to
these Protocols only — obtain instances via the factories in
``providers/__init__.py`` and never import a concrete provider or an AI SDK
directly.

The Protocols use structural typing (PEP 544): a provider implementation just
needs matching method signatures, no inheritance required.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable


@runtime_checkable
class TranscriptionProvider(Protocol):
    """Speech-to-text for a complete audio segment."""

    def transcribe(
        self,
        audio_wav: bytes,
        *,
        model: str,
        language: str | None = None,
        prompt: str | None = None,
    ) -> str:
        """Transcribe WAV audio bytes to text.

        Args:
            audio_wav: Complete WAV file contents.
            model: Provider-specific model identifier.
            language: ISO 639-1 hint for the spoken language; None = auto-detect.
            prompt: Optional tail of the immediately preceding transcript
                (same language as the audio) — conditions the model for
                cross-segment continuity. Providers may ignore it.

        Returns:
            The transcribed text.
        """
        ...


@runtime_checkable
class StreamHandle(Protocol):
    """An open duplex connection returned by ``StreamingTranscriptionProvider``."""

    def feed(self, pcm_bytes: bytes) -> None:
        """Push raw 16-bit PCM audio (mono) into the open stream."""
        ...

    def close(self) -> None:
        """Close the stream. Safe to call more than once."""
        ...

    def commit_turn(self) -> bool:
        """End the turn now and transcribe what the server already buffered.

        The non-destructive alternative to reopening a connection that has
        gone quiet: a speaker who never pauses long enough for server-side
        VAD produces one endless turn, and replacing the connection throws
        that buffered speech away unheard. Committing keeps it.

        Returns:
            True if a commit was actually issued. False means the provider
            has no such control and the caller must fall back to reopening
            the connection.
        """
        ...


@runtime_checkable
class StreamingTranscriptionProvider(Protocol):
    """Real-time speech-to-text over a persistent duplex connection.

    Distinct from ``TranscriptionProvider``: that one transcribes a complete
    pre-recorded segment and returns once; this one is fed a continuous audio
    stream and reports transcripts incrementally via callbacks as the provider
    produces them, for pipeline_mode="streaming" (see config.py / P7).
    """

    def open_stream(
        self,
        *,
        model: str,
        language: str | None,
        on_transcript: Callable[..., None],
        on_utterance_end: Callable[..., None],
        on_error: Callable[[Exception], None],
        on_speech_activity: Callable[[bool], None] | None = None,
    ) -> StreamHandle:
        """Open a streaming transcription session.

        Args:
            model: Provider-specific model identifier.
            language: Language hint; None = provider default (not necessarily
                the same auto-detect behavior as ``TranscriptionProvider``).
            on_transcript: Called with (text, is_final) as transcripts arrive.
                May be called multiple times per utterance with growing
                interim text before a final one. Providers whose engine can
                have more than one transcription unit open at once also pass
                ``item_id=`` — see ``on_utterance_end``.
            on_utterance_end: Called when the provider's own endpointing
                detects a natural pause — the signal to flush accumulated
                text into translation. Providers that transcribe in
                independently-completing units pass ``item_id=`` identifying
                the one that ended, because a unit completing is NOT proof the
                speaker stopped: OpenAI's forced turn commit opens a second
                item over audio the first still owns, and treating the first
                completion as the end of the utterance discarded the
                bookkeeping the second one was still relying on (duplicate
                subtitles, issue #106). Providers with a single linear stream
                omit it.
            on_error: Called if the connection fails or drops unexpectedly.
            on_speech_activity: Called with True when the provider's own VAD
                starts hearing speech and False when it stops. Separate from
                ``on_transcript`` because it arrives *while* a turn is still
                open — the only proof that a silent connection is working
                rather than stuck. Providers that expose no such signal leave
                it uncalled.

        Returns:
            A handle to feed audio into and close.
        """
        ...


@runtime_checkable
class TranslationProvider(Protocol):
    """Prompted text generation (translation, summarization)."""

    def complete(
        self,
        *,
        model: str,
        user_prompt: str,
        system_prompt: str | None = None,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        """Generate text from a system/user prompt pair.

        Returns:
            The generated text, stripped of surrounding whitespace.
        """
        ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Text embeddings for semantic similarity (RAG)."""

    def embed(self, text: str, *, model: str) -> list[float]:
        """Embed a single text.

        Returns:
            The embedding vector. Must be produced by the same model family
            as the precomputed Quran verse embeddings, otherwise RAG
            similarity scores are meaningless.
        """
        ...
