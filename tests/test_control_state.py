"""Tests for the control panel's Settings-derived rules (gui/control_state.py).

These rules used to be methods on AppGUI, where exercising them meant building
a whole Tk window — slow, display-bound and flaky. Extracted, they are plain
functions over a Settings object and test in milliseconds with no display.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from gui import control_state
from gui.control_state import (
    PROVIDER_PROFILE_CUSTOM,
    PROVIDER_PROFILE_GEMINI,
    PROVIDER_PROFILE_OPENAI,
    PROVIDER_ROLE_TRANSCRIPTION,
    PROVIDER_ROLE_TRANSLATION,
    PROVIDER_STATUS_CONFIGURED,
    PROVIDER_STATUS_ERROR,
    PROVIDER_STATUS_KEY_MISSING,
    PROVIDER_STATUS_RUNNING,
    PROVIDER_STATUSES,
    STRATEGY_IDS,
    apply_provider_profile,
    apply_strategy,
    current_strategy_index,
    effective_subtitle_mode,
    infer_provider_profile,
    provider_start_readiness,
    repair_default_provider,
    required_key_providers,
    subtitle_mode_choices,
    transcription_provider_for_profile,
    visible_provider_choices,
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
    SUBTITLE_MODE_STATIC,
    Settings,
)


def make_settings(**overrides) -> Settings:
    settings = Settings()
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


class TestNoTkImport:
    def test_module_does_not_pull_in_a_gui_toolkit(self):
        """The whole point of the split: these rules must stay importable and
        testable without a display. Checked against the parsed import
        statements — a text search also matches this module's own prose."""
        import ast

        tree = ast.parse(Path(control_state.__file__).read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)

        toolkits = [m for m in imported if m.split(".")[0] in ("tkinter", "customtkinter")]
        assert not toolkits, f"control_state must stay Tk-free, imports: {toolkits}"


class TestSubtitleModeChoices:
    def test_realtime_offered_while_streaming(self):
        settings = make_settings(pipeline_mode=PIPELINE_MODE_STREAMING)
        assert SUBTITLE_MODE_REALTIME in subtitle_mode_choices(settings)

    def test_realtime_hidden_while_segmented(self):
        settings = make_settings(pipeline_mode=PIPELINE_MODE_SEGMENTED)
        choices = subtitle_mode_choices(settings)
        assert SUBTITLE_MODE_REALTIME not in choices
        assert SUBTITLE_MODE_CONTINUOUS in choices
        assert SUBTITLE_MODE_STATIC in choices


class TestEffectiveSubtitleMode:
    def test_stored_realtime_falls_back_to_continuous_when_segmented(self):
        settings = make_settings(
            subtitle_mode=SUBTITLE_MODE_REALTIME,
            pipeline_mode=PIPELINE_MODE_SEGMENTED,
        )
        assert effective_subtitle_mode(settings) == SUBTITLE_MODE_CONTINUOUS

    def test_the_stored_choice_is_never_overwritten(self):
        """The fallback is display-only: Realtime must come back the moment
        streaming is selected again."""
        settings = make_settings(
            subtitle_mode=SUBTITLE_MODE_REALTIME,
            pipeline_mode=PIPELINE_MODE_SEGMENTED,
        )
        effective_subtitle_mode(settings)
        assert settings.subtitle_mode == SUBTITLE_MODE_REALTIME

        settings.pipeline_mode = PIPELINE_MODE_STREAMING
        assert effective_subtitle_mode(settings) == SUBTITLE_MODE_REALTIME

    @pytest.mark.parametrize("mode", [SUBTITLE_MODE_CONTINUOUS, SUBTITLE_MODE_STATIC])
    @pytest.mark.parametrize(
        "pipeline", [PIPELINE_MODE_SEGMENTED, PIPELINE_MODE_STREAMING]
    )
    def test_other_modes_pass_through_unchanged(self, mode, pipeline):
        settings = make_settings(subtitle_mode=mode, pipeline_mode=pipeline)
        assert effective_subtitle_mode(settings) == mode


class TestRequiredKeyProviders:
    """Keys are per provider, never per strategy — a realtime engine id must
    resolve to the provider whose key it authenticates with, or Start
    re-prompts for a key the user already has."""

    @pytest.mark.parametrize(
        ("engine", "expected"),
        [
            ("openai_realtime", "openai"),
            ("gemini_realtime", "gemini"),
            ("deepgram", "deepgram"),
            ("openai", "openai"),
            ("gemini", "gemini"),
        ],
    )
    def test_engine_maps_to_its_key_provider(self, engine, expected):
        settings = make_settings(ai_provider="anthropic", transcription_provider=engine)
        assert required_key_providers(settings) == ["anthropic", expected]

    def test_pseudo_provider_ids_never_leak_out(self):
        settings = make_settings(
            ai_provider="openai", transcription_provider="openai_realtime"
        )
        assert required_key_providers(settings) == ["openai"]

    def test_same_provider_for_both_roles_is_deduplicated(self):
        settings = make_settings(
            ai_provider="gemini", transcription_provider="gemini_realtime"
        )
        assert required_key_providers(settings) == ["gemini"]


class TestRepairDefaultProvider:
    @pytest.fixture(autouse=True)
    def _pin_resolution(self, monkeypatch):
        """Never consult the real keychain: the outcome must not depend on
        which keys the machine running the suite has."""
        monkeypatch.setattr(
            control_state, "resolve_provider_by_keys", lambda **k: DEFAULT_AI_PROVIDER
        )

    def test_unreachable_state_is_repaired(self):
        settings = make_settings(
            ai_provider="anthropic", use_default_translation_model=True
        )
        stale = repair_default_provider(settings)
        assert stale == "anthropic"
        assert settings.ai_provider == DEFAULT_AI_PROVIDER
        assert settings.use_default_translation_model is True

    def test_explicit_choice_is_left_alone(self):
        settings = make_settings(
            ai_provider="anthropic", use_default_translation_model=False
        )
        assert repair_default_provider(settings) is None
        assert settings.ai_provider == "anthropic"

    def test_consistent_default_is_left_alone(self):
        settings = make_settings(
            ai_provider=DEFAULT_AI_PROVIDER, use_default_translation_model=True
        )
        assert repair_default_provider(settings) is None
        assert settings.ai_provider == DEFAULT_AI_PROVIDER

    @pytest.mark.parametrize(
        ("pipeline", "transcription_provider"),
        [
            (PIPELINE_MODE_STREAMING, "openai_realtime"),
            (PIPELINE_MODE_SEGMENTED, "openai"),
        ],
    )
    def test_explicit_openai_profile_survives_startup_repair(
        self, pipeline, transcription_provider
    ):
        settings = make_settings(
            pipeline_mode=pipeline,
            ai_provider="openai",
            transcription_provider=transcription_provider,
            use_default_translation_model=True,
            use_default_transcription_model=True,
        )
        before = vars(settings).copy()

        assert repair_default_provider(settings) is None
        assert vars(settings) == before
        assert infer_provider_profile(settings) == PROVIDER_PROFILE_OPENAI

    def test_repair_to_a_non_default_provider_turns_the_default_off(
        self, monkeypatch
    ):
        """An OpenAI-era setup (only an OpenAI key) must keep working rather
        than being force-migrated to the current default."""
        monkeypatch.setattr(
            control_state, "resolve_provider_by_keys", lambda **k: "openai"
        )
        settings = make_settings(
            ai_provider="anthropic", use_default_translation_model=True
        )
        assert repair_default_provider(settings) == "anthropic"
        assert settings.ai_provider == "openai"
        assert settings.use_default_translation_model is False


class TestStrategySelection:
    def test_streaming_engine_reads_as_realtime(self):
        settings = make_settings(
            transcription_provider=DEFAULT_STREAMING_TRANSCRIPTION_PROVIDER,
            processing_strategy="chunk",
        )
        assert STRATEGY_IDS[current_strategy_index(settings)] == "realtime"

    @pytest.mark.parametrize("strategy", ["chunk", "semantic"])
    def test_segmented_reads_as_its_strategy(self, strategy):
        settings = make_settings(
            transcription_provider=DEFAULT_SEGMENTED_TRANSCRIPTION_PROVIDER,
            processing_strategy=strategy,
        )
        assert STRATEGY_IDS[current_strategy_index(settings)] == strategy

    def test_unknown_strategy_falls_back_to_chunk(self):
        settings = make_settings(
            transcription_provider=DEFAULT_SEGMENTED_TRANSCRIPTION_PROVIDER,
            processing_strategy="nonsense-from-a-future-version",
        )
        assert STRATEGY_IDS[current_strategy_index(settings)] == "chunk"

    def test_selecting_realtime_switches_to_a_streaming_engine(self):
        settings = make_settings(transcription_provider="gemini")
        applied = apply_strategy(settings, STRATEGY_IDS.index("realtime"))
        assert applied == "realtime"
        assert settings.pipeline_mode == PIPELINE_MODE_STREAMING
        assert settings.transcription_provider in STREAMING_TRANSCRIPTION_PROVIDERS

    def test_selecting_realtime_keeps_an_already_streaming_engine(self):
        settings = make_settings(transcription_provider="deepgram")
        apply_strategy(settings, STRATEGY_IDS.index("realtime"))
        assert settings.transcription_provider == "deepgram", (
            "an engine the user already chose must not be replaced"
        )

    def test_selecting_realtime_keeps_the_openai_service_family(self):
        settings = make_settings(
            ai_provider="openai", transcription_provider="openai"
        )
        apply_strategy(settings, STRATEGY_IDS.index("realtime"))
        assert settings.transcription_provider == "openai_realtime"

    @pytest.mark.parametrize("strategy", ["chunk", "semantic"])
    def test_selecting_segmented_leaves_streaming(self, strategy):
        settings = make_settings(transcription_provider="deepgram")
        applied = apply_strategy(settings, STRATEGY_IDS.index(strategy))
        assert applied == strategy
        assert settings.pipeline_mode == PIPELINE_MODE_SEGMENTED
        assert settings.processing_strategy == strategy
        assert settings.transcription_provider == (
            DEFAULT_SEGMENTED_TRANSCRIPTION_PROVIDER
        )

    def test_selecting_segmented_keeps_a_segmented_engine(self):
        settings = make_settings(transcription_provider="openai")
        apply_strategy(settings, STRATEGY_IDS.index("chunk"))
        assert settings.transcription_provider == "openai"

    def test_leaving_realtime_keeps_the_openai_service_family(self):
        settings = make_settings(
            ai_provider="openai", transcription_provider="openai_realtime"
        )
        apply_strategy(settings, STRATEGY_IDS.index("semantic"))
        assert settings.transcription_provider == "openai"

    @pytest.mark.parametrize("index", [-1, 3, 99])
    def test_out_of_range_index_changes_nothing(self, index):
        settings = make_settings(
            transcription_provider="gemini", pipeline_mode=PIPELINE_MODE_SEGMENTED
        )
        assert apply_strategy(settings, index) is None
        assert settings.transcription_provider == "gemini"
        assert settings.pipeline_mode == PIPELINE_MODE_SEGMENTED

    def test_strategy_round_trips_through_the_index(self):
        settings = make_settings()
        for strategy in STRATEGY_IDS:
            apply_strategy(settings, STRATEGY_IDS.index(strategy))
            assert STRATEGY_IDS[current_strategy_index(settings)] == strategy


class TestVisibleProviderChoices:
    CHOICES = [("OpenAI", "openai"), ("Gemini", "gemini"), ("Claude", "anthropic")]

    def test_stopped_shows_every_provider(self, monkeypatch):
        monkeypatch.setattr(control_state, "has_usable_key", lambda _p: False)
        assert visible_provider_choices(self.CHOICES, running=False) == self.CHOICES

    def test_running_hides_keyless_providers(self, monkeypatch):
        monkeypatch.setattr(
            control_state, "has_usable_key", lambda p: p == "gemini"
        )
        assert visible_provider_choices(self.CHOICES, running=True) == [
            ("Gemini", "gemini")
        ]

    def test_running_never_returns_an_empty_dropdown(self, monkeypatch):
        """Safety net: an empty list would leave the user with no selection at
        all, which is worse than showing everything."""
        monkeypatch.setattr(control_state, "has_usable_key", lambda _p: False)
        assert visible_provider_choices(self.CHOICES, running=True) == self.CHOICES

    def test_returns_a_copy_not_the_caller_s_list(self):
        result = visible_provider_choices(self.CHOICES, running=False)
        result.clear()
        assert self.CHOICES, "the caller's list must not be mutated"


class TestProviderProfiles:
    @pytest.mark.parametrize(
        ("profile", "pipeline", "expected_transcription"),
        [
            (PROVIDER_PROFILE_GEMINI, PIPELINE_MODE_STREAMING, "gemini_realtime"),
            (PROVIDER_PROFILE_GEMINI, PIPELINE_MODE_SEGMENTED, "gemini"),
            (PROVIDER_PROFILE_OPENAI, PIPELINE_MODE_STREAMING, "openai_realtime"),
            (PROVIDER_PROFILE_OPENAI, PIPELINE_MODE_SEGMENTED, "openai"),
        ],
    )
    def test_strategy_aware_transcription_mapping(
        self, profile, pipeline, expected_transcription
    ):
        assert (
            transcription_provider_for_profile(profile, pipeline)
            == expected_transcription
        )

    @pytest.mark.parametrize("profile", [PROVIDER_PROFILE_CUSTOM, "unknown"])
    def test_custom_and_unknown_profiles_have_no_implied_engine(self, profile):
        with pytest.raises(ValueError):
            transcription_provider_for_profile(profile, PIPELINE_MODE_STREAMING)

    @pytest.mark.parametrize(
        ("profile", "pipeline", "transcription"),
        [
            (PROVIDER_PROFILE_GEMINI, PIPELINE_MODE_STREAMING, "gemini_realtime"),
            (PROVIDER_PROFILE_GEMINI, PIPELINE_MODE_SEGMENTED, "gemini"),
            (PROVIDER_PROFILE_OPENAI, PIPELINE_MODE_STREAMING, "openai_realtime"),
            (PROVIDER_PROFILE_OPENAI, PIPELINE_MODE_SEGMENTED, "openai"),
        ],
    )
    def test_infers_simple_profile_only_for_an_exact_strategy_match(
        self, profile, pipeline, transcription
    ):
        settings = make_settings(
            ai_provider=profile,
            transcription_provider=transcription,
            pipeline_mode=pipeline,
        )
        assert infer_provider_profile(settings) == profile

    @pytest.mark.parametrize(
        ("translation", "transcription", "pipeline"),
        [
            ("openai", "gemini_realtime", PIPELINE_MODE_STREAMING),
            ("anthropic", "deepgram", PIPELINE_MODE_STREAMING),
            ("gemini", "gemini", PIPELINE_MODE_STREAMING),
            ("openai", "openai_realtime", PIPELINE_MODE_SEGMENTED),
        ],
    )
    def test_mixed_or_strategy_mismatched_configuration_is_custom(
        self, translation, transcription, pipeline
    ):
        settings = make_settings(
            ai_provider=translation,
            transcription_provider=transcription,
            pipeline_mode=pipeline,
        )
        assert infer_provider_profile(settings) == PROVIDER_PROFILE_CUSTOM

    @pytest.mark.parametrize(
        ("profile", "pipeline", "expected_transcription"),
        [
            (PROVIDER_PROFILE_GEMINI, PIPELINE_MODE_STREAMING, "gemini_realtime"),
            (PROVIDER_PROFILE_GEMINI, PIPELINE_MODE_SEGMENTED, "gemini"),
            (PROVIDER_PROFILE_OPENAI, PIPELINE_MODE_STREAMING, "openai_realtime"),
            (PROVIDER_PROFILE_OPENAI, PIPELINE_MODE_SEGMENTED, "openai"),
        ],
    )
    def test_applying_simple_profile_sets_both_roles_and_compatible_models(
        self, profile, pipeline, expected_transcription
    ):
        settings = make_settings(
            ai_provider="anthropic",
            transcription_provider="deepgram",
            pipeline_mode=pipeline,
            translation_model="old-translation-model",
            transcription_model="old-transcription-model",
            use_default_translation_model=False,
            use_default_transcription_model=False,
        )

        assert apply_provider_profile(settings, profile) == profile

        assert settings.ai_provider == profile
        assert settings.transcription_provider == expected_transcription
        assert settings.translation_model == control_state.get_default_model(
            profile, "translation"
        )
        assert settings.transcription_model == control_state.get_default_model(
            expected_transcription, "transcription"
        )
        assert settings.use_default_translation_model is True
        assert settings.use_default_transcription_model is True
        assert infer_provider_profile(settings) == profile

    def test_applying_custom_preserves_the_complete_mixed_configuration(self):
        settings = make_settings(
            ai_provider="anthropic",
            transcription_provider="deepgram",
            pipeline_mode=PIPELINE_MODE_STREAMING,
            translation_model="claude-custom",
            transcription_model="nova-custom",
            use_default_translation_model=False,
            use_default_transcription_model=False,
        )
        before = vars(settings).copy()

        assert apply_provider_profile(settings, PROVIDER_PROFILE_CUSTOM) == (
            PROVIDER_PROFILE_CUSTOM
        )
        assert vars(settings) == before

    def test_unknown_profile_is_rejected_without_mutation(self):
        settings = make_settings(ai_provider="openai", transcription_provider="openai")
        before = vars(settings).copy()
        assert apply_provider_profile(settings, "future-provider") is None
        assert vars(settings) == before


class TestProviderStartReadiness:
    def test_one_shared_key_is_looked_up_once_for_both_roles(self):
        calls: list[str] = []

        def lookup(provider):
            calls.append(provider)
            return True

        settings = make_settings(
            ai_provider="gemini", transcription_provider="gemini_realtime"
        )
        readiness = provider_start_readiness(settings, key_lookup=lookup)

        assert calls == ["gemini"]
        assert readiness.can_start is True
        assert readiness.blockers == ()
        assert readiness.missing_key_providers == ()
        assert readiness.status == PROVIDER_STATUS_CONFIGURED
        assert [role.status for role in readiness.roles] == [
            PROVIDER_STATUS_CONFIGURED,
            PROVIDER_STATUS_CONFIGURED,
        ]

    def test_openai_key_does_not_make_active_gemini_roles_ready(self):
        """Regression: saving an OpenAI key while Gemini remains selected
        must identify Gemini, not call the stored OpenAI key invalid."""
        settings = make_settings(
            ai_provider="gemini", transcription_provider="gemini_realtime"
        )
        readiness = provider_start_readiness(
            settings, key_lookup=lambda provider: provider == "openai"
        )

        assert readiness.can_start is False
        assert readiness.status == PROVIDER_STATUS_KEY_MISSING
        assert readiness.missing_key_providers == ("gemini",)
        assert {blocker.role for blocker in readiness.blockers} == {
            PROVIDER_ROLE_TRANSLATION,
            PROVIDER_ROLE_TRANSCRIPTION,
        }
        assert {blocker.key_provider_id for blocker in readiness.blockers} == {
            "gemini"
        }

    def test_mixed_providers_report_each_role_and_only_the_missing_key(self):
        settings = make_settings(
            ai_provider="anthropic", transcription_provider="openai_realtime"
        )
        readiness = provider_start_readiness(
            settings, key_lookup=lambda provider: provider == "anthropic"
        )
        by_role = {role.role: role for role in readiness.roles}

        assert readiness.profile_id == PROVIDER_PROFILE_CUSTOM
        assert by_role[PROVIDER_ROLE_TRANSLATION].key_provider_id == "anthropic"
        assert by_role[PROVIDER_ROLE_TRANSLATION].status == (
            PROVIDER_STATUS_CONFIGURED
        )
        assert by_role[PROVIDER_ROLE_TRANSCRIPTION].provider_id == "openai_realtime"
        assert by_role[PROVIDER_ROLE_TRANSCRIPTION].key_provider_id == "openai"
        assert by_role[PROVIDER_ROLE_TRANSCRIPTION].status == (
            PROVIDER_STATUS_KEY_MISSING
        )
        assert readiness.missing_key_providers == ("openai",)

    def test_running_and_error_are_explicit_factual_states(self):
        settings = make_settings(
            ai_provider="openai", transcription_provider="openai_realtime"
        )
        readiness = provider_start_readiness(
            settings,
            running=True,
            error_roles=(PROVIDER_ROLE_TRANSCRIPTION,),
            key_lookup=lambda _provider: True,
        )
        by_role = {role.role: role.status for role in readiness.roles}

        assert by_role == {
            PROVIDER_ROLE_TRANSLATION: PROVIDER_STATUS_RUNNING,
            PROVIDER_ROLE_TRANSCRIPTION: PROVIDER_STATUS_ERROR,
        }
        assert readiness.status == PROVIDER_STATUS_ERROR
        assert readiness.can_start is True

    def test_public_status_vocabulary_contains_no_validation_claims(self):
        assert set(PROVIDER_STATUSES) == {
            "configured",
            "key_missing",
            "running",
            "error",
        }
        assert not ({"verified", "connected", "valid"} & set(PROVIDER_STATUSES))


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
