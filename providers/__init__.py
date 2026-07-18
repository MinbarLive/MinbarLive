"""Factories for the configured AI provider.

Pipeline code gets provider instances and model chains from here based on the
user's ``ai_provider`` setting. Unknown values fall back to OpenAI so a stale
or hand-edited settings file can never disable the app.

Provider capability notes:
- Anthropic has no audio transcription API — the transcription factory
  falls back to the default provider when Anthropic is selected. The
  registry lookup handles this: a provider missing from a capability
  registry falls back to the default with a one-time warning.
- Embeddings must match the precomputed Quran verse embedding space; the
  embedding provider is therefore pinned to OpenAI regardless of
  ``ai_provider`` (see CLAUDE.md Priority 1).
"""

from __future__ import annotations

import os

import providers.anthropic as anthropic_models
import providers.deepgram as deepgram_models
import providers.gemini as gemini_models
import providers.gemini.realtime as gemini_live_models
import providers.openai.realtime as openai_realtime_models
from config import (
    EMBEDDING_MODEL,
    FS,
    GEMINI_EMBEDDING_MODEL,
    QURAN_EMBEDDINGS_GEMINI_NPZ_PATH,
)
from providers.anthropic import AnthropicTranslationProvider
from providers.base import (
    EmbeddingProvider,
    StreamingTranscriptionProvider,
    TranscriptionProvider,
    TranslationProvider,
)
from providers.deepgram import DeepgramTranscriptionProvider
from providers.gemini import (
    GeminiEmbeddingProvider,
    GeminiLiveTranscriptionProvider,
    GeminiTranscriptionProvider,
    GeminiTranslationProvider,
)
from providers.openai import (
    OpenAIEmbeddingProvider,
    OpenAIRealtimeTranscriptionProvider,
    OpenAITranscriptionProvider,
    OpenAITranslationProvider,
)
from utils.logging import log
from utils.settings import (
    DEFAULT_AI_PROVIDER,
    DEFAULT_STREAMING_TRANSCRIPTION_PROVIDER,
    DEFAULT_TRANSCRIPTION_MODEL,
    DEFAULT_TRANSLATION_MODEL,
    FALLBACK_TRANSCRIPTION_MODELS,
    FALLBACK_TRANSLATION_MODELS,
    TRANSCRIPTION_MODELS,
    TRANSLATION_MODELS,
    load_settings,
)

# Registered providers per capability. Instances are stateless, so one
# shared instance per provider is enough.
_TRANSCRIPTION_PROVIDERS: dict[str, TranscriptionProvider] = {
    "openai": OpenAITranscriptionProvider(),
    "gemini": GeminiTranscriptionProvider(),
}
_TRANSLATION_PROVIDERS: dict[str, TranslationProvider] = {
    "openai": OpenAITranslationProvider(),
    "gemini": GeminiTranslationProvider(),
    "anthropic": AnthropicTranslationProvider(),
}
_EMBEDDING_PROVIDERS: dict[str, EmbeddingProvider] = {
    "openai": OpenAIEmbeddingProvider(),
    "gemini": GeminiEmbeddingProvider(),
}

# Real-time streaming transcription (pipeline_mode="streaming", P7 phase 1).
# Keyed by the streaming ids in settings.STREAMING_TRANSCRIPTION_PROVIDERS.
# Kept separate from _TRANSCRIPTION_PROVIDERS above: streaming engines have a
# different Protocol (push-audio/callback) than whole-segment transcription.
# "openai_realtime"/"gemini_realtime" share the regular OpenAI/Gemini keys
# (see _STREAMING_KEY_PROVIDERS — no separate keyring entries).
_STREAMING_TRANSCRIPTION_PROVIDERS: dict[str, StreamingTranscriptionProvider] = {
    "deepgram": DeepgramTranscriptionProvider(),
    "openai_realtime": OpenAIRealtimeTranscriptionProvider(),
    "gemini_realtime": GeminiLiveTranscriptionProvider(),
}

# Which API key each streaming engine authenticates with — the realtime
# engines reuse their provider's regular key (no separate keyring entry).
_STREAMING_KEY_PROVIDERS = {
    "deepgram": "deepgram",
    "openai_realtime": "openai",
    "gemini_realtime": "gemini",
}

# Per-engine streaming model catalog: (default_model, dropdown choices).
_STREAMING_MODELS: dict[str, tuple[str, list[tuple[str, str]]]] = {
    "deepgram": (
        deepgram_models.DEFAULT_STREAMING_MODEL,
        deepgram_models.TRANSCRIPTION_MODELS,
    ),
    "openai_realtime": (
        openai_realtime_models.DEFAULT_REALTIME_MODEL,
        openai_realtime_models.TRANSCRIPTION_MODELS,
    ),
    "gemini_realtime": (
        gemini_live_models.DEFAULT_REALTIME_MODEL,
        gemini_live_models.TRANSCRIPTION_MODELS,
    ),
}

# Capture sample rate per streaming engine. Deepgram and Gemini Live take
# 16 kHz (FS); the OpenAI Realtime API only accepts 24 kHz PCM, so its
# capture must run at that rate.
_STREAMING_CAPTURE_RATES = {
    "deepgram": FS,
    "openai_realtime": openai_realtime_models.CAPTURE_SAMPLE_RATE,
    "gemini_realtime": FS,
}

DEFAULT_PROVIDER = "gemini"

# Fallback preference when a configured provider lacks a capability (e.g.
# Anthropic has no STT) or is unknown: the highest-ranked provider the user
# actually holds a key for, so a Gemini-less setup never "falls back" to a
# provider it cannot authenticate with. Explicit user choices are never
# overridden by this ranking — it applies to fallback paths only.
PROVIDER_RANKING = ["gemini", "openai", "anthropic"]

# Per-provider model chains: (default, fallbacks) per capability. The user's
# model setting is only honored when it belongs to the active provider —
# switching providers must not send e.g. "gpt-5.2" to Gemini.
_MODEL_CHAINS: dict[str, dict[str, tuple[str, list[str]]]] = {
    "openai": {
        "translation": (DEFAULT_TRANSLATION_MODEL, FALLBACK_TRANSLATION_MODELS),
        "transcription": (
            DEFAULT_TRANSCRIPTION_MODEL,
            FALLBACK_TRANSCRIPTION_MODELS,
        ),
    },
    "gemini": {
        "translation": (
            gemini_models.DEFAULT_TRANSLATION_MODEL,
            gemini_models.FALLBACK_TRANSLATION_MODELS,
        ),
        "transcription": (
            gemini_models.DEFAULT_TRANSCRIPTION_MODEL,
            gemini_models.FALLBACK_TRANSCRIPTION_MODELS,
        ),
    },
    "anthropic": {
        "translation": (
            anthropic_models.DEFAULT_TRANSLATION_MODEL,
            anthropic_models.FALLBACK_TRANSLATION_MODELS,
        ),
        # No Anthropic STT API — transcription runs on the OpenAI fallback,
        # so the GUI shows (and the wizard stores) the OpenAI models here.
        "transcription": (
            DEFAULT_TRANSCRIPTION_MODEL,
            FALLBACK_TRANSCRIPTION_MODELS,
        ),
    },
}

# Model ids each provider accepts (defaults + fallbacks + GUI dropdown lists).
_KNOWN_MODELS: dict[str, set[str]] = {
    "openai": (
        {model_id for _, model_id in TRANSLATION_MODELS}
        | {model_id for _, model_id in TRANSCRIPTION_MODELS}
        | set(FALLBACK_TRANSLATION_MODELS)
        | set(FALLBACK_TRANSCRIPTION_MODELS)
    ),
    "gemini": (
        set(gemini_models.FALLBACK_TRANSLATION_MODELS)
        | set(gemini_models.FALLBACK_TRANSCRIPTION_MODELS)
    ),
    "anthropic": (
        set(anthropic_models.FALLBACK_TRANSLATION_MODELS)
        | {model_id for _, model_id in anthropic_models.TRANSLATION_MODELS}
    ),
}


def _configured_provider() -> str:
    """The configured translation provider (ai_provider setting)."""
    return (load_settings().ai_provider or DEFAULT_PROVIDER).lower()


def _configured_transcription_provider() -> str:
    """The configured segmented-transcription provider.

    Distinct from ai_provider: the STT engine is chosen independently. Only
    openai/gemini reach the segmented registry — deepgram means streaming,
    which never calls get_transcription_provider().
    """
    return (
        getattr(load_settings(), "transcription_provider", None) or DEFAULT_PROVIDER
    ).lower()


# Capability fallbacks already warned about — _resolve runs on every audio
# segment, so the warning must not repeat every few seconds.
_warned_fallbacks: set[tuple[str, str]] = set()
# Resolved fallback targets, cached because has_usable_key can hit the OS
# keyring and _resolve runs per segment. Cleared when a key is saved/removed.
_fallback_cache: dict[tuple[str, str], str] = {}


def ranked_keyed_provider(candidates: list[str]) -> str:
    """The first candidate with a usable key; the first candidate if none."""
    for pid in candidates:
        if has_usable_key(pid):
            return pid
    return candidates[0]


def resolve_provider_by_keys(extra_keys: dict[str, str] | None = None) -> str:
    """Key-decided translation provider ("Standard" semantics).

    The app default wins whenever its key exists — or no provider has one at
    all; otherwise the highest-ranked provider with a usable key is chosen.
    `extra_keys` are keys not persisted yet (typed during onboarding).

    Deliberately checks has_configured_key(), not has_usable_key(): an
    ambient environment variable (GEMINI_API_KEY, OPENAI_API_KEY, ...) left
    over from some unrelated tool must not make this silently pick a
    provider the user never actually configured in MinbarLive. The env-var
    fallback still works for authenticating calls once a provider is
    actually selected — it just can't drive the selection itself.
    """
    extra = extra_keys or {}

    def _keyed(pid: str) -> bool:
        return bool((extra.get(pid) or "").strip()) or has_configured_key(pid)

    if _keyed(DEFAULT_AI_PROVIDER):
        return DEFAULT_AI_PROVIDER
    for pid in PROVIDER_RANKING:
        if _keyed(pid):
            return pid
    return DEFAULT_AI_PROVIDER


def _resolve(registry: dict, capability: str, name: str) -> tuple[str, object]:
    """Resolve a provider for a capability: (name, instance).

    A provider missing from the registry falls back to the highest-ranked
    provider in the registry with a usable key (see PROVIDER_RANKING).
    """
    provider = registry.get(name)
    if provider is None:
        cache_key = (name, capability)
        fallback = _fallback_cache.get(cache_key)
        if fallback is None or fallback not in registry:
            fallback = ranked_keyed_provider(
                [p for p in PROVIDER_RANKING if p in registry]
            )
            _fallback_cache[cache_key] = fallback
        if cache_key not in _warned_fallbacks:
            _warned_fallbacks.add(cache_key)
            log(
                f"AI provider '{name}' does not support {capability} — "
                f"falling back to '{fallback}'.",
                level="WARNING",
            )
        name, provider = fallback, registry[fallback]
    return name, provider


def get_transcription_provider() -> TranscriptionProvider:
    """Transcription provider for the configured transcription_provider setting."""
    return _resolve(
        _TRANSCRIPTION_PROVIDERS,
        "transcription",
        _configured_transcription_provider(),
    )[1]


def get_streaming_transcription_provider() -> StreamingTranscriptionProvider:
    """Streaming transcription provider for pipeline_mode="streaming".

    Resolved from the transcription_provider setting (the same single source
    of truth that derives pipeline_mode); a non-streaming or unknown value
    falls back to the default streaming engine.
    """
    provider_id = (
        getattr(load_settings(), "transcription_provider", None) or ""
    ).lower()
    return _STREAMING_TRANSCRIPTION_PROVIDERS.get(
        provider_id,
        _STREAMING_TRANSCRIPTION_PROVIDERS[DEFAULT_STREAMING_TRANSCRIPTION_PROVIDER],
    )


def get_streaming_key_provider(provider_id: str) -> str:
    """The key/provider id a streaming engine authenticates with (e.g. the
    "openai_realtime" engine uses the "openai" key). Non-streaming ids map to
    themselves, so this is safe to apply to any provider id."""
    return _STREAMING_KEY_PROVIDERS.get(provider_id, provider_id)


def get_streaming_capture_sample_rate(provider_id: str) -> int:
    """Microphone capture rate for a streaming engine (Hz)."""
    return _STREAMING_CAPTURE_RATES.get(provider_id, FS)


def get_translation_provider() -> TranslationProvider:
    """Translation/text-generation provider for the configured ai_provider setting."""
    return _resolve(_TRANSLATION_PROVIDERS, "translation", _configured_provider())[1]


def get_translation_provider_for(provider_id: str) -> TranslationProvider:
    """A specific translation/text-generation provider by id (e.g. the one the
    user picks in the summary dialog), falling back to the default provider
    when the id is unknown or lacks translation support."""
    return _resolve(
        _TRANSLATION_PROVIDERS, "translation", (provider_id or DEFAULT_PROVIDER).lower()
    )[1]


def get_transcription_provider_for(provider_id: str) -> TranscriptionProvider:
    """A specific whole-segment transcription provider by id (e.g. the one the
    user picks for a batch run), falling back to the default when the id is
    unknown or has no segment transcription (e.g. Deepgram is streaming-only)."""
    return _resolve(
        _TRANSCRIPTION_PROVIDERS,
        "transcription",
        (provider_id or DEFAULT_PROVIDER).lower(),
    )[1]


def get_embedding_space() -> str:
    """Which embedding space RAG runs in: "gemini" or "openai".

    Query embeddings must live in the same vector space as the precomputed
    verse matrix, so provider and verse file always switch TOGETHER: the
    Gemini space is used only when ai_provider is gemini AND its verse
    matrix exists (built via notebooks/build_embeddings_npz.py with
    PROVIDER="gemini"). Everything else uses the shipped OpenAI space.
    """
    if _configured_provider() == "gemini" and os.path.exists(
        QURAN_EMBEDDINGS_GEMINI_NPZ_PATH
    ):
        return "gemini"
    return "openai"  # the shipped fallback space, regardless of app default


def get_embedding_provider() -> EmbeddingProvider:
    """Embedding provider matching the active embedding space (see
    get_embedding_space)."""
    return _EMBEDDING_PROVIDERS[get_embedding_space()]


def get_embedding_model() -> str:
    """The embedding model id matching the active embedding space."""
    return (
        GEMINI_EMBEDDING_MODEL
        if get_embedding_space() == "gemini"
        else EMBEDDING_MODEL
    )


def _model_chain(
    registry: dict, capability: str, preferred: str, name: str
) -> list[str]:
    name, _ = _resolve(registry, capability, name)
    default, fallbacks = _MODEL_CHAINS[name][capability]
    preferred = (preferred or "").strip()
    primary = preferred if preferred in _KNOWN_MODELS[name] else default

    chain: list[str] = []
    for model in [primary, *fallbacks]:
        if model not in chain:
            chain.append(model)
    return chain


def get_translation_model_chain() -> list[str]:
    """Ordered models to try for translation with the active provider.

    The user's translation_model setting leads the chain only when it belongs
    to the active provider; otherwise the provider default does.
    """
    return _model_chain(
        _TRANSLATION_PROVIDERS,
        "translation",
        load_settings().translation_model,
        _configured_provider(),
    )


def get_transcription_model_chain() -> list[str]:
    """Ordered models to try for transcription with the active provider."""
    return _model_chain(
        _TRANSCRIPTION_PROVIDERS,
        "transcription",
        load_settings().transcription_model,
        _configured_transcription_provider(),
    )


def get_translation_model_chain_for(
    provider_id: str, model: str | None = None
) -> list[str]:
    """Translation model chain for an explicit provider (batch per-run choice).
    ``model`` leads the chain when it belongs to that provider."""
    return _model_chain(
        _TRANSLATION_PROVIDERS,
        "translation",
        model or "",
        (provider_id or DEFAULT_PROVIDER).lower(),
    )


def get_transcription_model_chain_for(
    provider_id: str, model: str | None = None
) -> list[str]:
    """Transcription model chain for an explicit provider (batch per-run choice)."""
    return _model_chain(
        _TRANSCRIPTION_PROVIDERS,
        "transcription",
        model or "",
        (provider_id or DEFAULT_PROVIDER).lower(),
    )


def resolve_streaming_transcription_model(
    provider_id: str, transcription_model: str
) -> str:
    """The streaming model to open the socket with, given the streaming engine
    and a stored ``transcription_model``: the value itself when it is a valid
    model for that engine, else the engine's default. Guards against a model
    id left over from another provider being sent to the wrong engine."""
    default, choices = _STREAMING_MODELS.get(
        provider_id, _STREAMING_MODELS[DEFAULT_STREAMING_TRANSCRIPTION_PROVIDER]
    )
    valid = {model_id for _name, model_id in choices}
    return transcription_model if transcription_model in valid else default


# ---------------------------------------------------------------------------
# GUI support: provider/model choice lists and per-provider key handling
# ---------------------------------------------------------------------------

# (display_name, provider_id) for the translation-provider dropdown
PROVIDER_CHOICES = [
    ("Google Gemini", "gemini"),
    ("OpenAI", "openai"),
    ("Anthropic Claude", "anthropic"),
]

# (display_name, provider_id) for the transcription-provider dropdown.
# The "(real-time)" entries are streaming engines; the others run the
# segmented pipeline.
TRANSCRIPTION_PROVIDER_CHOICES = [
    ("Google Gemini", "gemini"),
    ("OpenAI", "openai"),
    ("Google Gemini (real-time)", "gemini_realtime"),
    ("OpenAI (real-time)", "openai_realtime"),
    ("Deepgram (real-time)", "deepgram"),
]

# Model choices for the Deepgram streaming dropdown (Nova-3 default, Nova-2
# alternative). The socket uses one fixed model for its lifetime, so the GUI
# locks the dropdown only while streaming is running, not merely selected.
_DEEPGRAM_MODEL_CHOICES = deepgram_models.TRANSCRIPTION_MODELS

_MODEL_CHOICES: dict[str, dict[str, list[tuple[str, str]]]] = {
    "openai": {
        "translation": TRANSLATION_MODELS,
        "transcription": TRANSCRIPTION_MODELS,
    },
    "gemini": {
        "translation": gemini_models.TRANSLATION_MODELS,
        "transcription": gemini_models.TRANSCRIPTION_MODELS,
    },
    "anthropic": {
        "translation": anthropic_models.TRANSLATION_MODELS,
        # Transcription falls back to OpenAI (no Anthropic STT API).
        "transcription": TRANSCRIPTION_MODELS,
    },
    "deepgram": {
        "transcription": _DEEPGRAM_MODEL_CHOICES,
    },
    "openai_realtime": {
        "transcription": openai_realtime_models.TRANSCRIPTION_MODELS,
    },
    "gemini_realtime": {
        "transcription": gemini_live_models.TRANSCRIPTION_MODELS,
    },
}


def _effective_capability_provider(provider: str, capability: str) -> str:
    """The provider whose models the GUI should show for a capability.

    A known translation-only provider (Anthropic) has no STT of its own —
    show the models of the key-aware fallback engine that will actually run
    instead of a hardcoded OpenAI list. Streaming and unknown ids keep the
    existing static lookup.
    """
    if (
        capability == "transcription"
        and provider in _TRANSLATION_PROVIDERS
        and provider not in _TRANSCRIPTION_PROVIDERS
    ):
        return _resolve(_TRANSCRIPTION_PROVIDERS, capability, provider)[0]
    return provider


def get_model_choices(provider: str, capability: str) -> list[tuple[str, str]]:
    """(display_name, model_id) dropdown choices for a provider capability."""
    provider = _effective_capability_provider(provider, capability)
    per_provider = _MODEL_CHOICES.get(provider, _MODEL_CHOICES[DEFAULT_PROVIDER])
    return per_provider.get(
        capability, _MODEL_CHOICES[DEFAULT_PROVIDER][capability]
    )


def get_default_model(provider: str, capability: str) -> str:
    """The default model id for a provider capability."""
    if provider in _STREAMING_MODELS and capability == "transcription":
        return _STREAMING_MODELS[provider][0]
    provider = _effective_capability_provider(provider, capability)
    chains = _MODEL_CHAINS.get(provider, _MODEL_CHAINS[DEFAULT_PROVIDER])
    return chains[capability][0]


# Providers with an API key of their own. Each providers/<id>/client module
# exposes the same surface (set_api_key, has_api_key, _load_stored_key with
# keyring-then-environment lookup) — importing the module is cheap, the AI
# SDKs inside stay lazily imported. OpenAI is the exception: its persistence
# goes through utils.settings (legacy settings-file migration + the disclosed
# plaintext fallback when no keyring backend exists).
_KEYED_PROVIDERS = ("openai", "gemini", "anthropic", "deepgram")

# Env vars each provider's key lookup falls back to (get_stored_api_key above,
# and each providers/<id>/client._load_stored_key). load_dotenv() bakes these
# into os.environ for the whole process at startup, so clear_api_key() must
# also drop them here — otherwise a .env-sourced key keeps authenticating for
# the rest of the running session no matter how many times it is "removed".
_ENV_VARS_FOR_PROVIDER = {
    "openai": ("OPENAI_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "deepgram": ("DEEPGRAM_API_KEY",),
}


def _client_module(provider: str):
    import importlib

    return importlib.import_module(f"providers.{provider}.client")


def has_insecure_key_fallback(provider: str) -> bool:
    """True if this provider still persists its key when no keychain exists.

    Only OpenAI does, via the legacy plaintext settings-file fallback. Every
    other provider's key is session-only in that case — it is gone after a
    restart. save_api_key() returns False for both outcomes, so callers that
    report the result to the user need this to tell "saved, but in plaintext"
    apart from "not saved at all".
    """
    return provider == "openai"


def get_stored_api_key(provider: str) -> str | None:
    """A provider's persisted key (keychain, legacy file, or environment)."""
    import os

    if provider == "openai":
        from utils.settings import get_saved_api_key

        key = get_saved_api_key() or os.getenv("OPENAI_API_KEY")
    elif provider in _KEYED_PROVIDERS:
        key = _client_module(provider)._load_stored_key()
    else:
        return None
    return (key or "").strip() or None


def has_usable_key(provider: str) -> bool:
    """True if the provider has a key available (in memory or stored)."""
    if provider not in _KEYED_PROVIDERS:
        return False
    module = _client_module(provider)
    return module.has_api_key() or get_stored_api_key(provider) is not None


def has_configured_key(provider: str) -> bool:
    """True if a key was explicitly saved for this provider through MinbarLive
    (OS keychain, or the legacy openai settings-file fallback).

    Unlike has_usable_key()/get_stored_api_key(), this deliberately ignores
    the GEMINI_API_KEY/OPENAI_API_KEY/ANTHROPIC_API_KEY/DEEPGRAM_API_KEY
    environment-variable fallback each provider's client module also checks
    — an ambient env var left over from some unrelated tool is a real,
    working credential (fine for actually authenticating a call), but it is
    not evidence the user configured that provider in MinbarLive, so it must
    not count for provider-selection decisions (resolve_provider_by_keys).
    """
    if provider not in _KEYED_PROVIDERS:
        return False
    if provider == "openai":
        from utils.settings import get_saved_api_key

        return get_saved_api_key() is not None
    from utils.keyring_storage import get_api_key_from_keyring

    return get_api_key_from_keyring(provider) is not None


def save_api_key(provider: str, key: str) -> bool:
    """Persist a provider's API key and activate it for the session.

    Returns:
        True if stored securely (keychain); False if only a fallback or
        session-only storage was possible.
    """
    key = (key or "").strip()
    if not key or provider not in _KEYED_PROVIDERS:
        return False

    if provider == "openai":
        # set_saved_api_key handles keyring + legacy settings-file fallback
        from utils.settings import set_saved_api_key

        stored_securely = set_saved_api_key(key)
    else:
        from utils.keyring_storage import set_api_key_in_keyring

        stored_securely = set_api_key_in_keyring(key, provider)
    # Activate for this session even when persistence fell back / failed
    _client_module(provider).set_api_key(key)
    # Key-aware fallback targets may change now — re-resolve (and re-warn
    # once with the new target) on next use.
    _fallback_cache.clear()
    _warned_fallbacks.clear()
    return stored_securely


def clear_api_key(provider: str) -> None:
    """Delete a provider's stored key and deactivate it for the session."""
    if provider not in _KEYED_PROVIDERS:
        return

    if provider == "openai":
        from utils.settings import delete_saved_api_key

        delete_saved_api_key()  # keychain + legacy settings-file cleanup
    else:
        from utils.keyring_storage import delete_api_key_from_keyring

        delete_api_key_from_keyring(provider)
    _client_module(provider).set_api_key(None)
    for env_var in _ENV_VARS_FOR_PROVIDER.get(provider, ()):
        os.environ.pop(env_var, None)
    _fallback_cache.clear()
    _warned_fallbacks.clear()
