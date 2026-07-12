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

__all__ = [
    "DEFAULT_STREAMING_MODEL",
    "TRANSCRIPTION_MODELS",
    "DeepgramStreamHandle",
    "DeepgramTranscriptionProvider",
]
