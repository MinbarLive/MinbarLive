"""Control-panel decisions derived from Settings — deliberately Tk-free.

These rules (which providers need a key, which subtitle modes are offered,
what a Processing Strategy choice does to the settings) are policy, not
presentation: they read and write a ``Settings`` object and never touch a
widget. They used to live as methods on ``AppGUI``, which meant the only way
to exercise them was to build an entire window — so in practice they were
never tested at all.

Keeping them here means the rules can be unit-tested headlessly, and the
mixin in gui/app_gui.py is left with what actually needs Tk: reading the
dropdowns, repainting them, and prompting for keys.

Nothing in this module may import tkinter/customtkinter.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

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

# The simple provider profile is intentionally inferred from the existing
# provider fields rather than persisted as a second source of truth.  A user
# can therefore move between old and new UI versions without a settings
# migration, and mixed/expert configurations remain lossless.
PROVIDER_PROFILE_GEMINI = "gemini"
PROVIDER_PROFILE_OPENAI = "openai"
PROVIDER_PROFILE_CUSTOM = "custom"
PROVIDER_PROFILE_IDS = (
    PROVIDER_PROFILE_GEMINI,
    PROVIDER_PROFILE_OPENAI,
    PROVIDER_PROFILE_CUSTOM,
)

PROVIDER_ROLE_TRANSLATION = "translation"
PROVIDER_ROLE_TRANSCRIPTION = "transcription"

# These are factual UI states, not claims that an external service has been
# contacted or verified.  In particular, a locally stored key is only
# "present"; it is never described as connected or valid.
PROVIDER_STATUS_CONFIGURED = "configured"
PROVIDER_STATUS_KEY_MISSING = "key_missing"
PROVIDER_STATUS_RUNNING = "running"
PROVIDER_STATUS_ERROR = "error"
PROVIDER_STATUSES = (
    PROVIDER_STATUS_CONFIGURED,
    PROVIDER_STATUS_KEY_MISSING,
    PROVIDER_STATUS_RUNNING,
    PROVIDER_STATUS_ERROR,
)

START_BLOCKER_MISSING_KEY = "missing_key"


@dataclass(frozen=True)
class ProviderRoleReadiness:
    """Key and runtime readiness for one active provider role.

    ``provider_id`` is the configured engine (for example
    ``gemini_realtime``), while ``key_provider_id`` is the credential owner
    (``gemini``).  Keeping both prevents the controller from asking for a
    fictitious ``gemini_realtime`` key and lets the view name the exact role
    that is blocked.
    """

    role: str
    provider_id: str
    key_provider_id: str
    key_present: bool
    status: str

    @property
    def ready(self) -> bool:
        return self.key_present


@dataclass(frozen=True)
class StartBlocker:
    """A machine-readable reason why Start must remain unavailable."""

    code: str
    role: str
    provider_id: str
    key_provider_id: str


@dataclass(frozen=True)
class StartReadiness:
    """Complete provider preflight for the V3 control dashboard."""

    profile_id: str
    roles: tuple[ProviderRoleReadiness, ...]
    blockers: tuple[StartBlocker, ...]

    @property
    def can_start(self) -> bool:
        return not self.blockers

    @property
    def missing_key_providers(self) -> tuple[str, ...]:
        """Credential owners to prompt, de-duplicated in role order."""
        providers: list[str] = []
        for blocker in self.blockers:
            if (
                blocker.code == START_BLOCKER_MISSING_KEY
                and blocker.key_provider_id not in providers
            ):
                providers.append(blocker.key_provider_id)
        return tuple(providers)

    @property
    def status(self) -> str:
        """One factual summary state for the primary action area."""
        statuses = {role.status for role in self.roles}
        if PROVIDER_STATUS_ERROR in statuses:
            return PROVIDER_STATUS_ERROR
        if self.blockers:
            return PROVIDER_STATUS_KEY_MISSING
        if PROVIDER_STATUS_RUNNING in statuses:
            return PROVIDER_STATUS_RUNNING
        return PROVIDER_STATUS_CONFIGURED


def transcription_provider_for_profile(profile_id: str, pipeline_mode: str) -> str:
    """Return the STT engine represented by a simple service profile.

    The same profile deliberately maps to different engine ids depending on
    the processing strategy because real-time engines have separate registry
    entries but share their provider's credential.

    Raises ``ValueError`` for ``custom`` (which has no implied provider) and
    unknown ids; callers applying custom mode must preserve the current
    fields instead.
    """
    if profile_id not in (PROVIDER_PROFILE_GEMINI, PROVIDER_PROFILE_OPENAI):
        raise ValueError(f"No implied transcription provider for {profile_id!r}")
    if pipeline_mode == PIPELINE_MODE_STREAMING:
        return f"{profile_id}_realtime"
    return profile_id


def infer_provider_profile(settings: Settings) -> str:
    """Infer Gemini/OpenAI simple mode, otherwise return ``custom``.

    Exact engine matching is intentional.  A mixed provider setup, a
    Deepgram/Anthropic setup, or a strategy/provider mismatch is expert state
    and must not be silently normalized merely by opening the control panel.
    """
    for profile_id in (PROVIDER_PROFILE_GEMINI, PROVIDER_PROFILE_OPENAI):
        if settings.ai_provider != profile_id:
            continue
        expected_transcription = transcription_provider_for_profile(
            profile_id, settings.pipeline_mode
        )
        if settings.transcription_provider == expected_transcription:
            return profile_id
    return PROVIDER_PROFILE_CUSTOM


def apply_provider_profile(settings: Settings, profile_id: str) -> str | None:
    """Apply a simple provider profile without adding persisted settings.

    Selecting ``custom`` is a view-mode choice and therefore leaves every
    provider/model field untouched.  Selecting Gemini or OpenAI makes both
    roles consistent with the active strategy and resets each model to the
    selected provider's shipped default.  Returns the applied id, or ``None``
    for an unknown id without mutating ``settings``.
    """
    if profile_id not in PROVIDER_PROFILE_IDS:
        return None
    if profile_id == PROVIDER_PROFILE_CUSTOM:
        return profile_id

    transcription_provider = transcription_provider_for_profile(
        profile_id, settings.pipeline_mode
    )
    settings.ai_provider = profile_id
    settings.transcription_provider = transcription_provider
    settings.translation_model = get_default_model(profile_id, "translation")
    settings.transcription_model = get_default_model(
        transcription_provider, "transcription"
    )
    settings.use_default_translation_model = True
    settings.use_default_transcription_model = True
    return profile_id


def provider_start_readiness(
    settings: Settings,
    *,
    running: bool = False,
    error_roles: Iterable[str] = (),
    key_lookup: Callable[[str], bool] | None = None,
) -> StartReadiness:
    """Build the provider/key preflight consumed by the V3 dashboard.

    ``key_lookup`` is injectable so tests and callers performing a cached
    refresh never need to touch a real keychain.  Results are cached per
    credential owner within this call, since one key often serves both roles.
    ``error_roles`` marks factual runtime errors already known by the caller;
    this function does not attempt a network validation.
    """
    lookup = key_lookup or has_usable_key
    error_role_set = set(error_roles)
    key_cache: dict[str, bool] = {}
    configured_roles = (
        (PROVIDER_ROLE_TRANSLATION, settings.ai_provider),
        (PROVIDER_ROLE_TRANSCRIPTION, settings.transcription_provider),
    )
    roles: list[ProviderRoleReadiness] = []
    blockers: list[StartBlocker] = []

    for role, provider_id in configured_roles:
        key_provider_id = get_streaming_key_provider(provider_id)
        if key_provider_id not in key_cache:
            key_cache[key_provider_id] = bool(lookup(key_provider_id))
        key_present = key_cache[key_provider_id]

        if role in error_role_set:
            status = PROVIDER_STATUS_ERROR
        elif not key_present:
            status = PROVIDER_STATUS_KEY_MISSING
        elif running:
            status = PROVIDER_STATUS_RUNNING
        else:
            status = PROVIDER_STATUS_CONFIGURED

        roles.append(
            ProviderRoleReadiness(
                role=role,
                provider_id=provider_id,
                key_provider_id=key_provider_id,
                key_present=key_present,
                status=status,
            )
        )
        if not key_present:
            blockers.append(
                StartBlocker(
                    code=START_BLOCKER_MISSING_KEY,
                    role=role,
                    provider_id=provider_id,
                    key_provider_id=key_provider_id,
                )
            )

    return StartReadiness(
        profile_id=infer_provider_profile(settings),
        roles=tuple(roles),
        blockers=tuple(blockers),
    )


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
    "Use default" is on.  V3's simple Gemini/OpenAI profiles, however,
    deliberately select each provider's own default models and therefore also
    set that flag.  A coherent simple profile is an explicit user choice and
    must never be replaced by key ranking during startup.

    For genuinely inconsistent legacy/custom state, keys still decide,
    mirroring onboarding: the default provider wins when its key exists or
    none is stored at all; otherwise the highest-ranked keyed provider is kept
    with "Use default" off.

    Mutates ``settings`` in place. Returns the stale provider that was
    replaced, or None when nothing needed repairing (so the caller can decide
    whether to persist and log).
    """
    if infer_provider_profile(settings) != PROVIDER_PROFILE_CUSTOM:
        return None
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
            current_family = get_streaming_key_provider(
                settings.transcription_provider
            )
            if current_family in (
                PROVIDER_PROFILE_GEMINI,
                PROVIDER_PROFILE_OPENAI,
            ):
                settings.transcription_provider = transcription_provider_for_profile(
                    current_family, PIPELINE_MODE_STREAMING
                )
            else:
                settings.transcription_provider = (
                    DEFAULT_STREAMING_TRANSCRIPTION_PROVIDER
                )
        settings.pipeline_mode = PIPELINE_MODE_STREAMING
    else:
        settings.processing_strategy = selection
        settings.pipeline_mode = PIPELINE_MODE_SEGMENTED
        if settings.transcription_provider in STREAMING_TRANSCRIPTION_PROVIDERS:
            current_family = get_streaming_key_provider(
                settings.transcription_provider
            )
            if current_family in (
                PROVIDER_PROFILE_GEMINI,
                PROVIDER_PROFILE_OPENAI,
            ):
                settings.transcription_provider = current_family
            elif settings.ai_provider in (
                PROVIDER_PROFILE_GEMINI,
                PROVIDER_PROFILE_OPENAI,
            ):
                settings.transcription_provider = settings.ai_provider
            else:
                settings.transcription_provider = (
                    DEFAULT_SEGMENTED_TRANSCRIPTION_PROVIDER
                )
    return selection


def visible_provider_choices(
    choices: list[tuple[str, str]],
    running: bool,
    *,
    key_lookup: Callable[[str], bool] | None = None,
) -> list[tuple[str, str]]:
    """While the pipeline is RUNNING, only providers with a saved key are
    offered — switching to a keyless provider mid-run would break the pipeline
    (it re-reads the provider per translation / audio segment). Stopped, all
    are shown so the user can pick one and add its key. Never empty: the
    active provider always has a key (required at start).
    """
    if not running:
        return list(choices)
    lookup = key_lookup or has_usable_key
    keyed = [
        (n, p) for n, p in choices if lookup(get_streaming_key_provider(p))
    ]
    return keyed or list(choices)
