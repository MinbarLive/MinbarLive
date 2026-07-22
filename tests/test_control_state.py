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
    STRATEGY_IDS,
    apply_strategy,
    current_strategy_index,
    effective_subtitle_mode,
    repair_default_provider,
    required_key_providers,
    subtitle_mode_choices,
    visible_provider_choices,
)
from providers import PROVIDER_RANKING
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

    def test_repair_to_a_non_default_provider_turns_the_default_off(
        self, monkeypatch
    ):
        """A setup whose only key belongs to a non-default provider must keep
        working rather than being force-migrated to the current default —
        and "Use default" has to go off so the panel shows the real one."""
        # Whichever provider is NOT the default today; the rule is about the
        # relationship, not about a particular vendor.
        keyed = next(p for p in PROVIDER_RANKING if p != DEFAULT_AI_PROVIDER)
        monkeypatch.setattr(
            control_state, "resolve_provider_by_keys", lambda **k: keyed
        )
        stale = next(p for p in PROVIDER_RANKING if p not in (DEFAULT_AI_PROVIDER, keyed))
        settings = make_settings(
            ai_provider=stale, use_default_translation_model=True
        )
        assert repair_default_provider(settings) == stale
        assert settings.ai_provider == keyed
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
