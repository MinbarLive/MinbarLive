"""Control-panel decisions derived from Settings — deliberately toolkit-free.

These rules (which providers need a key, which subtitle modes are offered,
what a Processing Strategy choice does to the settings) are policy, not
presentation: they read and write a ``Settings`` object and never touch a
widget. They used to live as methods on the panel class, which meant the only
way to exercise them was to build an entire window — so in practice they were
never tested at all.

Keeping them here means the rules unit-test headlessly in milliseconds, and the
panel is left with what genuinely needs widgets: reading the dropdowns,
repainting them, and prompting for keys. New control-panel logic that only
reads or writes ``Settings`` belongs in this file.

**Nothing in this module may import a GUI toolkit** — a test imports it in a
subprocess and asserts none appears in ``sys.modules``.
"""

from __future__ import annotations

from providers import (
    get_default_model,
    get_streaming_key_provider,
    has_usable_key,
    resolve_provider_by_keys,
)
from utils.settings import (
    DEFAULT_AI_PROVIDER,
    DEFAULT_SEGMENTED_TRANSCRIPTION_PROVIDER,
    DEFAULT_STREAMING_TRANSCRIPTION_PROVIDER,
    PIPELINE_MODE_SEGMENTED,
    PIPELINE_MODE_STREAMING,
    STREAMING_TRANSCRIPTION_PROVIDERS,
    SUBTITLE_MODE_CONTINUOUS,
    SUBTITLE_MODE_REALTIME,
    SUBTITLE_MODES,
    Settings,
)

# Processing Strategy dropdown entries, in display order.
STRATEGY_IDS = ["realtime", "semantic", "chunk"]


def subtitle_mode_choices(settings: Settings) -> list[str]:
    """Modes offered in the Subtitles dropdown. Realtime (the live feed with
    the in-progress transcript line) is streaming-only."""
    if settings.pipeline_mode == PIPELINE_MODE_STREAMING:
        return list(SUBTITLE_MODES)
    return [m for m in SUBTITLE_MODES if m != SUBTITLE_MODE_REALTIME]


def effective_subtitle_mode(settings: Settings) -> str:
    """The display mode the subtitle window should actually use.

    A stored Realtime mode falls back to continuous under a segmented
    strategy. The stored value is deliberately left alone — Realtime returns
    the moment streaming is re-selected.
    """
    mode = settings.subtitle_mode
    if mode == SUBTITLE_MODE_REALTIME and settings.pipeline_mode != (
        PIPELINE_MODE_STREAMING
    ):
        return SUBTITLE_MODE_CONTINUOUS
    return mode


def required_key_providers(settings: Settings) -> list[str]:
    """Providers that must have a key before the pipeline can start: the
    translation LLM and the transcription engine (de-duplicated).

    Streaming engine ids map to the provider whose key they authenticate with
    (openai_realtime -> openai) — keys are per provider, never per strategy, so
    an existing OpenAI key must never be re-prompted just because real-time
    mode is selected.
    """
    providers: list[str] = []
    for provider in (
        settings.ai_provider,
        get_streaming_key_provider(settings.transcription_provider),
    ):
        if provider and provider not in providers:
            providers.append(provider)
    return providers


def repair_default_provider(settings: Settings) -> str | None:
    """Repair a stored "Use default" + non-default translation provider.

    Early onboarding wrote the last-BROWSED provider as ai_provider even when
    no key was ever entered for it; the provider dropdown is disabled while
    "Use default" is on, so the GUI itself can never produce (or leave) that
    state. Keys decide, mirroring onboarding: the default provider wins when
    its key exists or none is stored at all; otherwise the highest-ranked
    keyed provider is kept with "Use default" off.

    Mutates ``settings`` in place. Returns the stale provider that was
    replaced, or None when nothing needed repairing (so the caller can decide
    whether to persist and log).
    """
    if (
        not settings.use_default_translation_model
        or settings.ai_provider == DEFAULT_AI_PROVIDER
    ):
        return None
    stale = settings.ai_provider
    provider = resolve_provider_by_keys()
    settings.ai_provider = provider
    settings.translation_model = get_default_model(provider, "translation")
    settings.use_default_translation_model = provider == DEFAULT_AI_PROVIDER
    return stale


def current_strategy_index(settings: Settings) -> int:
    """Which Processing Strategy entry reflects the current settings."""
    if settings.transcription_provider in STREAMING_TRANSCRIPTION_PROVIDERS:
        return STRATEGY_IDS.index("realtime")
    strat = settings.processing_strategy
    if strat in STRATEGY_IDS:
        return STRATEGY_IDS.index(strat)
    return STRATEGY_IDS.index("chunk")  # segmented default


def apply_strategy(settings: Settings, index: int) -> str | None:
    """Apply a Processing Strategy dropdown choice to ``settings``.

    Real-time switches the transcription engine to a streaming one (keeping
    one that is already selected); chunk/semantic switch back to a segmented
    engine. Returns the applied strategy id, or None for an out-of-range
    index.
    """
    if not (0 <= index < len(STRATEGY_IDS)):
        return None
    selection = STRATEGY_IDS[index]
    if selection == "realtime":
        if settings.transcription_provider not in STREAMING_TRANSCRIPTION_PROVIDERS:
            settings.transcription_provider = DEFAULT_STREAMING_TRANSCRIPTION_PROVIDER
        settings.pipeline_mode = PIPELINE_MODE_STREAMING
    else:
        settings.processing_strategy = selection
        settings.pipeline_mode = PIPELINE_MODE_SEGMENTED
        if settings.transcription_provider in STREAMING_TRANSCRIPTION_PROVIDERS:
            settings.transcription_provider = DEFAULT_SEGMENTED_TRANSCRIPTION_PROVIDER
    return selection


def visible_provider_choices(
    choices: list[tuple[str, str]], running: bool
) -> list[tuple[str, str]]:
    """While the pipeline is RUNNING, only providers with a saved key are
    offered — switching to a keyless provider mid-run would break the pipeline
    (it re-reads the provider per translation / audio segment). Stopped, all
    are shown so the user can pick one and add its key. Never empty: the
    active provider always has a key (required at start).
    """
    if not running:
        return list(choices)
    keyed = [
        (n, p) for n, p in choices if has_usable_key(get_streaming_key_provider(p))
    ]
    return keyed or list(choices)
