"""Deepgram provider: real-time streaming transcription only (no translation)."""

from config import STREAMING_MODEL
from providers.deepgram.transcription import (
    DeepgramStreamHandle,
    DeepgramTranscriptionProvider,
)

DEFAULT_STREAMING_MODEL = STREAMING_MODEL

# (display_name, model_id) choices for the streaming-model dropdown. Nova-3 is
# the default (multilingual, best Arabic accuracy); Nova-2 is offered as an
# alternative. Both are current Deepgram real-time models.
TRANSCRIPTION_MODELS = [
    ("Deepgram Nova-3", "nova-3"),
    ("Deepgram Nova-2", "nova-2"),
]

# Source-language codes each model accepts, keyed by model id. Per MODEL, not
# per provider: Nova-2 has no Arabic at all while Nova-3 does, so collapsing
# these into one "Deepgram" set would offer the app's primary language on an
# engine that cannot transcribe it.
#
# Base ISO-639-1 codes only — Deepgram also publishes regional variants
# (ar-EG, en-GB, pt-BR …) but the app only ever sends the base code from
# SOURCE_LANGUAGES, so the variants would never match anything.
#
# Source: developers.deepgram.com/docs/models-languages-overview (2026-08-26).
SUPPORTED_LANGUAGE_CODES = {
    "nova-3": frozenset(
        {
            "af", "ar", "be", "bg", "bn", "bs", "ca", "cs", "da", "de",
            "el", "en", "es", "et", "fa", "fi", "fr", "gu", "he", "hi",
            "hr", "hu", "hy", "id", "it", "ja", "ka", "kn", "ko", "lt",
            "lv", "mk", "mr", "ms", "ne", "nl", "no", "pa", "pl", "pt",
            "ro", "ru", "sk", "sl", "sr", "sv", "ta", "te", "th", "tl",
            "tr", "uk", "ur", "vi", "zh",
        }
    ),
    "nova-2": frozenset(
        {
            "bg", "ca", "cs", "da", "de", "el", "en", "es", "et", "fi",
            "fr", "hi", "hu", "id", "it", "ja", "ko", "lt", "lv", "ms",
            "nl", "no", "pl", "pt", "ro", "ru", "sk", "sv", "th", "tr",
            "uk", "vi", "zh",
        }
    ),
}

__all__ = [
    "DEFAULT_STREAMING_MODEL",
    "SUPPORTED_LANGUAGE_CODES",
    "TRANSCRIPTION_MODELS",
    "DeepgramStreamHandle",
    "DeepgramTranscriptionProvider",
]
