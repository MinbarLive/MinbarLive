"""Gemini provider implementations and model defaults."""

from providers.gemini.embeddings import GeminiEmbeddingProvider
from providers.gemini.realtime import GeminiLiveTranscriptionProvider
from providers.gemini.transcription import GeminiTranscriptionProvider
from providers.gemini.translation import GeminiTranslationProvider

# Model chains for the provider registry in providers/__init__.py.
# Gemini uses the same multimodal models for text and audio input.
DEFAULT_TRANSLATION_MODEL = "gemini-2.5-flash"
FALLBACK_TRANSLATION_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.5-flash-lite",
]
DEFAULT_TRANSCRIPTION_MODEL = "gemini-2.5-flash"
FALLBACK_TRANSCRIPTION_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
]

# GUI dropdown lists (display_name, model_id), mirroring the OpenAI lists in
# utils/settings.py
TRANSLATION_MODELS = [
    ("Gemini 2.5 Flash (Recommended)", "gemini-2.5-flash"),
    ("Gemini 2.0 Flash", "gemini-2.0-flash"),
    ("Gemini 2.5 Flash Lite (Fastest)", "gemini-2.5-flash-lite"),
]
TRANSCRIPTION_MODELS = [
    ("Gemini 2.5 Flash (Recommended)", "gemini-2.5-flash"),
    ("Gemini 2.0 Flash", "gemini-2.0-flash"),
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
