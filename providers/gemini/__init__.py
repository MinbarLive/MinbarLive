"""Gemini provider implementations and model defaults."""

from providers.gemini.embeddings import GeminiEmbeddingProvider
from providers.gemini.realtime import GeminiLiveTranscriptionProvider
from providers.gemini.transcription import GeminiTranscriptionProvider
from providers.gemini.translation import GeminiTranslationProvider

# Model chains for the provider registry in providers/__init__.py.
# Gemini uses the same multimodal models for text and audio input.
#
# Live-probed 2026-07-15 (user's key): gemini-2.5-flash and -flash-lite now
# 404 ("no longer available to new users") and gemini-2.0-flash has zero
# quota on new accounts — all three removed. gemini-3.1-flash-lite verified
# 0.6s Arabic→German (honorifics kept) and char-exact Arabic STT;
# gemini-3.5-flash verified 2.3s translation / char-exact STT. Translation
# defaults to the lite model — subtitle latency IS the product; 3.5-flash is
# the quality opt-in. Transcription defaults to 3.5-flash (same ~3.4s as the
# lite on the probe, bigger model for hard mosque audio).
DEFAULT_TRANSLATION_MODEL = "gemini-3.1-flash-lite"
FALLBACK_TRANSLATION_MODELS = [
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash",
]
DEFAULT_TRANSCRIPTION_MODEL = "gemini-3.5-flash"
FALLBACK_TRANSCRIPTION_MODELS = [
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
]

# GUI dropdown lists (display_name, model_id), mirroring the OpenAI lists in
# utils/settings.py
TRANSLATION_MODELS = [
    ("Gemini 3.1 Flash Lite (Recommended, fastest)", "gemini-3.1-flash-lite"),
    ("Gemini 3.5 Flash (Highest quality)", "gemini-3.5-flash"),
]
TRANSCRIPTION_MODELS = [
    ("Gemini 3.5 Flash (Recommended)", "gemini-3.5-flash"),
    ("Gemini 3.1 Flash Lite (Faster)", "gemini-3.1-flash-lite"),
]

__all__ = [
    "DEFAULT_TRANSCRIPTION_MODEL",
    "DEFAULT_TRANSLATION_MODEL",
    "FALLBACK_TRANSCRIPTION_MODELS",
    "FALLBACK_TRANSLATION_MODELS",
    "GeminiEmbeddingProvider",
    "GeminiLiveTranscriptionProvider",
    "GeminiTranscriptionProvider",
    "GeminiTranslationProvider",
]
