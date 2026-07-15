"""Anthropic Claude provider implementation and model defaults.

Translation only: Anthropic has no audio transcription API, so the
transcription factory in providers/__init__.py falls back to the default
provider when Anthropic is selected.
"""

from providers.anthropic.translation import AnthropicTranslationProvider

# Model chains for the provider registry in providers/__init__.py.
DEFAULT_TRANSLATION_MODEL = "claude-sonnet-5"
FALLBACK_TRANSLATION_MODELS = [
    "claude-sonnet-5",
    "claude-haiku-4-5",
]

# GUI dropdown list (display_name, model_id), mirroring the OpenAI lists in
# utils/settings.py. Opus is offered as a choice but kept out of the fallback
# chain — at live-translation call rates its cost is a deliberate opt-in.
TRANSLATION_MODELS = [
    ("Claude Sonnet 5 (Recommended)", "claude-sonnet-5"),
    ("Claude Haiku 4.5 (Fastest)", "claude-haiku-4-5"),
    ("Claude Opus 4.8 (Highest quality)", "claude-opus-4-8"),
    # Listed on the account's models endpoint (2026-07-15); premium tier,
    # same cost opt-in treatment as Opus. Unverified live (no credits).
    ("Claude Fable 5 (Premium)", "claude-fable-5"),
]

__all__ = [
    "DEFAULT_TRANSLATION_MODEL",
    "FALLBACK_TRANSLATION_MODELS",
    "TRANSLATION_MODELS",
    "AnthropicTranslationProvider",
]
