"""Tests for settings management."""

import json
import sys
from pathlib import Path

import pytest

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

import utils.settings as settings_module
from utils.settings import (
    DEFAULT_STREAMING_TRANSCRIPTION_PROVIDER,
    DEFAULT_THEME_MODE,
    DEFAULT_TRANSCRIPTION_MODEL,
    DEFAULT_TRANSLATION_MODEL,
    PIPELINE_MODE_SEGMENTED,
    PIPELINE_MODE_STREAMING,
    PIPELINE_MODES,
    SOURCE_LANGUAGES,
    STREAMING_TRANSCRIPTION_PROVIDERS,
    SUBTITLE_MODE_CONTINUOUS,
    SUBTITLE_MODE_REALTIME,
    SUBTITLE_MODE_STATIC,
    SUBTITLE_MODES,
    TARGET_LANGUAGE_NAMES,
    Settings,
    get_source_language_code,
    get_target_language_code,
    language_canonical_name,
    language_display_name,
    load_settings,
    save_settings,
)


class TestSettingsDataclass:
    """Tests for Settings dataclass."""

    def test_default_values(self):
        """Settings should have sensible defaults."""
        settings = Settings()
        # Note: openai_api_key is now stored in keyring, not in Settings
        assert settings.monitor_index == 1
        assert settings.font_size_base == 40
        assert settings.source_language == "Automatic"
        assert settings.target_language == "German"
        assert settings.subtitle_mode == SUBTITLE_MODE_REALTIME
        assert settings.scroll_speed == 1.0
        assert settings.transparent_static is False
        assert settings.adaptive_subtitle_catchup is True
        assert settings.bilingual_mode is True  # default ON since 2026-07-15
        assert settings.islamic_mode is True
        assert settings.noise_filter is True
        assert settings.theme_mode == DEFAULT_THEME_MODE == "light"
        assert settings.translation_model == DEFAULT_TRANSLATION_MODEL
        assert settings.transcription_model == DEFAULT_TRANSCRIPTION_MODEL

    def test_custom_values(self):
        """Settings should accept custom values."""
        settings = Settings(
            monitor_index=0,
            font_size_base=50,
            source_language="Turkish",
            target_language="English",
            subtitle_mode=SUBTITLE_MODE_STATIC,
            scroll_speed=2.5,
            transparent_static=True,
            adaptive_subtitle_catchup=True,
            bilingual_mode=True,
            translation_model="gpt-4o",
            transcription_model="gpt-4o-mini-transcribe",
        )
        assert settings.monitor_index == 0
        assert settings.font_size_base == 50
        assert settings.source_language == "Turkish"
        assert settings.target_language == "English"
        assert settings.subtitle_mode == SUBTITLE_MODE_STATIC
        assert settings.scroll_speed == 2.5
        assert settings.transparent_static is True
        assert settings.adaptive_subtitle_catchup is True
        assert settings.bilingual_mode is True
        assert settings.translation_model == "gpt-4o"
        assert settings.transcription_model == "gpt-4o-mini-transcribe"


class TestSubtitleModes:
    """Tests for subtitle mode constants."""

    def test_mode_values(self):
        """Mode constants should have expected values."""
        assert SUBTITLE_MODE_CONTINUOUS == "continuous"
        assert SUBTITLE_MODE_STATIC == "static"

    def test_modes_list(self):
        """SUBTITLE_MODES should contain all modes (stack was removed;
        realtime was added July 2026)."""
        assert SUBTITLE_MODE_REALTIME in SUBTITLE_MODES
        assert SUBTITLE_MODE_CONTINUOUS in SUBTITLE_MODES
        assert SUBTITLE_MODE_STATIC in SUBTITLE_MODES
        assert len(SUBTITLE_MODES) == 3

    def test_stored_stack_mode_falls_back_to_continuous(self, tmp_path, monkeypatch):
        """Settings saved before the stack-mode removal must self-migrate."""
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"subtitle_mode": "stack"}), encoding="utf-8")
        monkeypatch.setattr(settings_module, "_settings_path", lambda: path)
        settings_module._cached_settings = None
        try:
            loaded = load_settings(use_cache=False)
            assert loaded.subtitle_mode == SUBTITLE_MODE_CONTINUOUS
        finally:
            settings_module._cached_settings = None


class TestApiKeyStorage:
    """The plaintext settings-file fallback was removed: a key is either in
    the OS keychain or session-only. Nothing writes it to disk any more, and
    a key left in settings.json by an older version is cleaned up on read.
    """

    def _no_keyring(self, monkeypatch):
        monkeypatch.setattr(
            "utils.keyring_storage._check_keyring_available", lambda: False
        )

    def test_save_without_keyring_writes_nothing(self, tmp_path, monkeypatch):
        self._no_keyring(monkeypatch)
        path = tmp_path / "settings.json"
        monkeypatch.setattr(settings_module, "_settings_path", lambda: path)

        assert settings_module.set_saved_api_key("sk-secret-123") is False
        assert not path.exists()

    def test_save_without_keyring_leaves_existing_file_clean(
        self, tmp_path, monkeypatch
    ):
        self._no_keyring(monkeypatch)
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"gui_language": "de"}), encoding="utf-8")
        monkeypatch.setattr(settings_module, "_settings_path", lambda: path)

        assert settings_module.set_saved_api_key("sk-secret-123") is False
        assert "sk-secret-123" not in path.read_text(encoding="utf-8")
        assert json.loads(path.read_text(encoding="utf-8")) == {"gui_language": "de"}

    def test_legacy_key_is_wiped_when_there_is_no_keyring(self, tmp_path, monkeypatch):
        """No keychain to migrate into: the plaintext key is deleted rather
        than kept, and the user re-enters it (session-only)."""
        self._no_keyring(monkeypatch)
        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps({"gui_language": "de", "openai_api_key": "sk-legacy-123"}),
            encoding="utf-8",
        )
        monkeypatch.setattr(settings_module, "_settings_path", lambda: path)

        assert settings_module.get_saved_api_key() is None
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "openai_api_key" not in data
        assert data["gui_language"] == "de"  # rest of the file survives

    def test_legacy_key_migrates_into_the_keyring(self, tmp_path, monkeypatch):
        stored: dict[str, str] = {}
        monkeypatch.setattr(
            "utils.keyring_storage._check_keyring_available", lambda: True
        )
        monkeypatch.setattr(
            "utils.keyring_storage.get_api_key_from_keyring",
            lambda provider="openai": stored.get(provider),
        )
        monkeypatch.setattr(
            "utils.keyring_storage.set_api_key_in_keyring",
            lambda key, provider="openai": stored.setdefault(provider, key) is not None,
        )
        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps({"openai_api_key": "sk-legacy-123"}), encoding="utf-8"
        )
        monkeypatch.setattr(settings_module, "_settings_path", lambda: path)

        assert settings_module.get_saved_api_key() == "sk-legacy-123"
        assert stored["openai"] == "sk-legacy-123"
        assert "openai_api_key" not in json.loads(path.read_text(encoding="utf-8"))


class TestSourceLanguageCode:
    """Tests for source language code lookup."""

    def test_arabic_code(self):
        """Arabic should return 'ar'."""
        assert get_source_language_code("Arabic") == "ar"

    def test_turkish_code(self):
        """Turkish should return 'tr'."""
        assert get_source_language_code("Turkish") == "tr"

    def test_auto_detect_code(self):
        """Auto-detect should return None."""
        assert get_source_language_code("Auto-detect") is None

    def test_unknown_language(self):
        """Unknown language should return None."""
        assert get_source_language_code("Klingon") is None

    def test_all_languages_have_codes(self):
        """All source languages should have a code defined."""
        for name, code in SOURCE_LANGUAGES:
            # Each entry should be a (name, code) tuple
            assert isinstance(name, str)
            assert code is None or isinstance(code, str)


class TestTargetLanguageNames:
    """Tests for target language names list."""

    def test_german_is_first(self):
        """German should be the first/default language."""
        assert TARGET_LANGUAGE_NAMES[0] == "German"

    def test_common_languages_present(self):
        """Common languages should be in the list."""
        assert "English" in TARGET_LANGUAGE_NAMES
        assert "Arabic" in TARGET_LANGUAGE_NAMES
        assert "Turkish" in TARGET_LANGUAGE_NAMES
        assert "French" in TARGET_LANGUAGE_NAMES

    def test_no_duplicates(self):
        """There should be no duplicate languages."""
        assert len(TARGET_LANGUAGE_NAMES) == len(set(TARGET_LANGUAGE_NAMES))


class TestSettingsEdgeCases:
    """Edge case tests for settings."""

    def test_settings_without_api_key(self):
        """Settings dataclass should not have openai_api_key (stored in keyring)."""
        settings = Settings()
        # Verify openai_api_key is not an attribute
        assert not hasattr(settings, "openai_api_key")

    def test_negative_monitor_index(self):
        """Negative monitor index should be accepted (validation elsewhere)."""
        settings = Settings(monitor_index=-1)
        assert settings.monitor_index == -1

    def test_extreme_scroll_speed(self):
        """Extreme scroll speed values should be accepted."""
        settings = Settings(scroll_speed=100.0)
        assert settings.scroll_speed == 100.0


class TestPipelineMode:
    """pipeline_mode is derived from transcription_provider (streaming
    engines => streaming); it is no longer a directly stored/selected value."""

    def test_defaults_to_streaming(self):
        # Fresh installs default to real-time streaming on OpenAI — one key
        # covers translation + transcription + RAG, and it is the only
        # streaming engine measured to keep up with realtime input (user
        # decision 2026-07-22, supersedes the 2026-07-14 Gemini default).
        assert DEFAULT_STREAMING_TRANSCRIPTION_PROVIDER == "openai_realtime"
        assert (
            Settings().transcription_provider
            == DEFAULT_STREAMING_TRANSCRIPTION_PROVIDER
        )
        assert Settings().pipeline_mode == PIPELINE_MODE_STREAMING

    def test_constants(self):
        assert PIPELINE_MODE_SEGMENTED == "segmented"
        assert PIPELINE_MODE_STREAMING == "streaming"
        assert PIPELINE_MODES == [PIPELINE_MODE_SEGMENTED, PIPELINE_MODE_STREAMING]

    @pytest.mark.parametrize("provider", STREAMING_TRANSCRIPTION_PROVIDERS)
    def test_streaming_transcription_provider_derives_streaming(
        self, tmp_path, monkeypatch, provider
    ):
        monkeypatch.setattr(
            settings_module, "_settings_path", lambda: tmp_path / "settings.json"
        )
        settings_module._cached_settings = None
        save_settings(Settings(transcription_provider=provider))
        settings_module._cached_settings = None
        try:
            loaded = load_settings(use_cache=False)
            assert loaded.transcription_provider == provider
            assert loaded.pipeline_mode == PIPELINE_MODE_STREAMING
        finally:
            settings_module._cached_settings = None

    def test_legacy_streaming_pipeline_mode_migrates_to_deepgram(
        self, tmp_path, monkeypatch
    ):
        # Settings written before the split had pipeline_mode="streaming" and
        # no transcription_provider — those were Deepgram sessions (the only
        # engine then), so they keep their engine rather than getting the
        # current default (openai_realtime).
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"pipeline_mode": "streaming"}), encoding="utf-8")
        monkeypatch.setattr(settings_module, "_settings_path", lambda: path)
        settings_module._cached_settings = None
        try:
            loaded = load_settings(use_cache=False)
            assert loaded.transcription_provider == "deepgram"
            assert loaded.pipeline_mode == PIPELINE_MODE_STREAMING
        finally:
            settings_module._cached_settings = None


class TestSkippedUpdateVersion:
    """A release the user passed over on the update notice."""

    def test_nothing_is_skipped_by_default(self):
        assert Settings().skipped_update_version == ""

    def test_round_trip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            settings_module, "_settings_path", lambda: tmp_path / "settings.json"
        )
        settings_module._cached_settings = None
        save_settings(Settings(skipped_update_version="1.2.0"))
        settings_module._cached_settings = None
        try:
            assert load_settings(use_cache=False).skipped_update_version == "1.2.0"
        finally:
            settings_module._cached_settings = None

    @pytest.mark.parametrize("stored", [None, 123, True, ["1.0.0"], {}])
    def test_a_non_string_never_reaches_the_comparison(
        self, tmp_path, monkeypatch, stored
    ):
        # It is compared against a GitHub tag. A junk value out of a hand-edited
        # file must not silence the update notice with nothing on screen to say
        # why.
        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps({"skipped_update_version": stored}), encoding="utf-8"
        )
        monkeypatch.setattr(settings_module, "_settings_path", lambda: path)
        settings_module._cached_settings = None
        try:
            assert load_settings(use_cache=False).skipped_update_version == ""
        finally:
            settings_module._cached_settings = None

    def test_a_stored_value_is_length_capped(self, tmp_path, monkeypatch):
        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps({"skipped_update_version": " " + "9" * 500 + " "}),
            encoding="utf-8",
        )
        monkeypatch.setattr(settings_module, "_settings_path", lambda: path)
        settings_module._cached_settings = None
        try:
            loaded = load_settings(use_cache=False).skipped_update_version
            assert len(loaded) == 64
            assert loaded == "9" * 64  # stripped before capping
        finally:
            settings_module._cached_settings = None


class TestReviewPromptSettings:
    """The session counter and the permanent off switch behind the review
    prompt (gui/review_banner.py)."""

    def test_defaults(self):
        assert Settings().sessions_since_review_prompt == 0
        assert Settings().review_prompt_disabled is False

    def test_round_trip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            settings_module, "_settings_path", lambda: tmp_path / "settings.json"
        )
        settings_module._cached_settings = None
        save_settings(
            Settings(sessions_since_review_prompt=2, review_prompt_disabled=True)
        )
        settings_module._cached_settings = None
        try:
            loaded = load_settings(use_cache=False)
            assert loaded.sessions_since_review_prompt == 2
            assert loaded.review_prompt_disabled is True
        finally:
            settings_module._cached_settings = None

    @pytest.mark.parametrize(
        ("stored", "expected"),
        [
            (-5, 0),  # clamped: a negative count would push the prompt out
            (True, 0),  # bool is an int in Python, and is not a count
            ("3", 0),
            (None, 0),
            (7, 7),  # already past the threshold stays past it
        ],
    )
    def test_the_counter_is_validated(self, tmp_path, monkeypatch, stored, expected):
        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps({"sessions_since_review_prompt": stored}), encoding="utf-8"
        )
        monkeypatch.setattr(settings_module, "_settings_path", lambda: path)
        settings_module._cached_settings = None
        try:
            loaded = load_settings(use_cache=False)
            assert loaded.sessions_since_review_prompt == expected
        finally:
            settings_module._cached_settings = None


class TestSubtitleHideMode:
    def test_default_is_never(self):
        assert Settings().subtitle_hide_mode == "never"

    def test_round_trip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            settings_module, "_settings_path", lambda: tmp_path / "settings.json"
        )
        settings_module._cached_settings = None
        save_settings(Settings(subtitle_hide_mode="stopped"))
        settings_module._cached_settings = None
        try:
            assert load_settings(use_cache=False).subtitle_hide_mode == "stopped"
        finally:
            settings_module._cached_settings = None

    def test_invalid_value_falls_back_to_never(self, tmp_path, monkeypatch):
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"subtitle_hide_mode": "nope"}), encoding="utf-8")
        monkeypatch.setattr(settings_module, "_settings_path", lambda: path)
        settings_module._cached_settings = None
        try:
            assert load_settings(use_cache=False).subtitle_hide_mode == "never"
        finally:
            settings_module._cached_settings = None

    def test_legacy_output_off_migrates_to_always(self, tmp_path, monkeypatch):
        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps({"subtitle_output_enabled": False}), encoding="utf-8"
        )
        monkeypatch.setattr(settings_module, "_settings_path", lambda: path)
        settings_module._cached_settings = None
        try:
            assert load_settings(use_cache=False).subtitle_hide_mode == "always"
        finally:
            settings_module._cached_settings = None

    def test_legacy_hide_on_stop_migrates_to_stopped(self, tmp_path, monkeypatch):
        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps({"hide_subtitle_on_stop": True}), encoding="utf-8"
        )
        monkeypatch.setattr(settings_module, "_settings_path", lambda: path)
        settings_module._cached_settings = None
        try:
            assert load_settings(use_cache=False).subtitle_hide_mode == "stopped"
        finally:
            settings_module._cached_settings = None


class TestWindowStyle:
    def test_default_is_integrated(self):
        # Integrated became the default in 033bd63; these two assertions were
        # left at the old value.
        assert Settings().window_style == "integrated"

    def test_round_trip_integrated(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            settings_module, "_settings_path", lambda: tmp_path / "settings.json"
        )
        settings_module._cached_settings = None
        save_settings(Settings(window_style="integrated"))
        settings_module._cached_settings = None
        try:
            assert load_settings(use_cache=False).window_style == "integrated"
        finally:
            settings_module._cached_settings = None

    def test_invalid_value_falls_back_to_the_default(self, tmp_path, monkeypatch):
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"window_style": "floating"}), encoding="utf-8")
        monkeypatch.setattr(settings_module, "_settings_path", lambda: path)
        settings_module._cached_settings = None
        try:
            assert load_settings(use_cache=False).window_style == "integrated"
        finally:
            settings_module._cached_settings = None


class TestStopAnnouncementOnLiveStop:
    def test_default_is_true(self):
        assert Settings().stop_announcement_on_live_stop is True

    def test_round_trip_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            settings_module, "_settings_path", lambda: tmp_path / "settings.json"
        )
        settings_module._cached_settings = None
        save_settings(Settings(stop_announcement_on_live_stop=False))
        settings_module._cached_settings = None
        try:
            loaded = load_settings(use_cache=False)
            assert loaded.stop_announcement_on_live_stop is False
        finally:
            settings_module._cached_settings = None

    def test_invalid_value_falls_back_to_true(self, tmp_path, monkeypatch):
        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps({"stop_announcement_on_live_stop": "nope"}), encoding="utf-8"
        )
        monkeypatch.setattr(settings_module, "_settings_path", lambda: path)
        settings_module._cached_settings = None
        try:
            loaded = load_settings(use_cache=False)
            assert loaded.stop_announcement_on_live_stop is True
        finally:
            settings_module._cached_settings = None


class TestSubtitleTypography:
    """Independent original-text size + optional subtitle colour overrides."""

    def test_defaults_preserve_the_historical_70_percent_look(self):
        s = Settings()
        assert s.source_font_size_base == pytest.approx(40 / 0.7)
        assert s.translation_text_color == ""
        assert s.source_text_color == ""

    def test_round_trip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            settings_module, "_settings_path", lambda: tmp_path / "settings.json"
        )
        settings_module._cached_settings = None
        save_settings(
            Settings(
                source_font_size_base=45.0,
                translation_text_color="#FF8800",
                source_text_color="#00AAFF",
            )
        )
        settings_module._cached_settings = None
        try:
            loaded = load_settings(use_cache=False)
            assert loaded.source_font_size_base == pytest.approx(45.0)
            assert loaded.translation_text_color == "#FF8800"
            assert loaded.source_text_color == "#00AAFF"
        finally:
            settings_module._cached_settings = None

    def test_missing_key_derives_70_percent_from_stored_font_size(
        self, tmp_path, monkeypatch
    ):
        """Settings files predating the setting keep their old appearance."""
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"font_size_base": 60}), encoding="utf-8")
        monkeypatch.setattr(settings_module, "_settings_path", lambda: path)
        settings_module._cached_settings = None
        try:
            loaded = load_settings(use_cache=False)
            assert loaded.source_font_size_base == pytest.approx(60 / 0.7)
        finally:
            settings_module._cached_settings = None

    @pytest.mark.parametrize(
        "stored,expected",
        [
            (5, 20.0),  # below the floor
            (500, 120.0),  # above the ceiling
            ("big", 40 / 0.7),  # not a number
            (True, 40 / 0.7),  # bool is not a size
            (float("nan"), 40 / 0.7),  # non-finite
        ],
    )
    def test_font_size_is_clamped_and_type_checked(
        self, tmp_path, monkeypatch, stored, expected
    ):
        path = tmp_path / "settings.json"
        # NaN is not valid JSON per spec but json.dumps emits it — and a
        # hand-edited file can contain anything, which is the point here.
        path.write_text(
            json.dumps({"source_font_size_base": stored}), encoding="utf-8"
        )
        monkeypatch.setattr(settings_module, "_settings_path", lambda: path)
        settings_module._cached_settings = None
        try:
            loaded = load_settings(use_cache=False)
            assert loaded.source_font_size_base == pytest.approx(expected)
        finally:
            settings_module._cached_settings = None

    def _loaded(self, tmp_path, monkeypatch, stored: dict):
        path = tmp_path / "settings.json"
        path.write_text(json.dumps(stored), encoding="utf-8")
        monkeypatch.setattr(settings_module, "_settings_path", lambda: path)
        settings_module._cached_settings = None
        try:
            return load_settings(use_cache=False)
        finally:
            settings_module._cached_settings = None

    @pytest.mark.parametrize(
        ("stored", "expected"), [(0, 5), (3, 5), (5, 5), (100, 100), (400, 100)]
    )
    def test_the_band_height_clamps_to_its_own_range(
        self, tmp_path, monkeypatch, stored, expected
    ):
        # A band thinner than 5% holds no text at all.
        loaded = self._loaded(tmp_path, monkeypatch, {"window_height_percent": stored})
        assert loaded.window_height_percent == expected

    @pytest.mark.parametrize(
        ("stored", "expected"), [(0, 0), (-10, 0), (30, 30), (50, 50), (90, 50)]
    )
    def test_the_lift_clamps_to_its_own_range_and_keeps_zero(
        self, tmp_path, monkeypatch, stored, expected
    ):
        """Its own field since 2026-08-07. Sharing the height's meant the
        height's floor of 5 rewrote "Abstand von unten: 0%" on every restart."""
        loaded = self._loaded(tmp_path, monkeypatch, {"static_lift_percent": stored})
        assert loaded.static_lift_percent == expected

    def test_the_two_meanings_no_longer_overwrite_each_other(
        self, tmp_path, monkeypatch
    ):
        loaded = self._loaded(
            tmp_path,
            monkeypatch,
            {"window_height_percent": 80, "static_lift_percent": 0},
        )
        assert (loaded.window_height_percent, loaded.static_lift_percent) == (80, 0)

    def test_a_file_from_before_the_split_gets_the_lift_default(
        self, tmp_path, monkeypatch
    ):
        """NOT migrated from the height field: that carries whichever meaning
        was written last, so reading it as a lift would hand anyone who left
        the slider on a band height a lift of up to 50% they never chose."""
        loaded = self._loaded(tmp_path, monkeypatch, {"window_height_percent": 80})
        assert loaded.static_lift_percent == 0
        assert loaded.window_height_percent == 80

    @pytest.mark.parametrize(
        "stored", ["", "red", "#FFF", "#GGGGGG", "#ff8800 ", 42, None]
    )
    def test_invalid_colour_falls_back_to_theme_default(
        self, tmp_path, monkeypatch, stored
    ):
        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps({"translation_text_color": stored}), encoding="utf-8"
        )
        monkeypatch.setattr(settings_module, "_settings_path", lambda: path)
        settings_module._cached_settings = None
        try:
            assert load_settings(use_cache=False).translation_text_color == ""
        finally:
            settings_module._cached_settings = None


class TestAnnouncementHistory:
    """The recent-announcement (megaphone) list persists to settings.json."""

    def test_default_is_empty(self):
        assert Settings().announcement_history == []

    def test_round_trip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            settings_module, "_settings_path", lambda: tmp_path / "settings.json"
        )
        settings_module._cached_settings = None
        save_settings(Settings(announcement_history=["Hello", "مرحبا"]))
        settings_module._cached_settings = None
        try:
            loaded = load_settings(use_cache=False)
            assert loaded.announcement_history == ["Hello", "مرحبا"]
        finally:
            settings_module._cached_settings = None

    def test_sanitizes_and_caps_on_load(self, tmp_path, monkeypatch):
        path = tmp_path / "settings.json"
        # Non-strings, blanks and an over-long list must be cleaned to ≤3 texts.
        path.write_text(
            json.dumps(
                {
                    "announcement_history": [
                        "one",
                        "  ",
                        2,
                        None,
                        "two",
                        "three",
                        "four",
                        "five",
                        "six",
                    ]
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(settings_module, "_settings_path", lambda: path)
        settings_module._cached_settings = None
        try:
            loaded = load_settings(use_cache=False)
            assert loaded.announcement_history == ["one", "two", "three"]
        finally:
            settings_module._cached_settings = None

    def test_non_list_falls_back_to_empty(self, tmp_path, monkeypatch):
        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps({"announcement_history": "not a list"}), encoding="utf-8"
        )
        monkeypatch.setattr(settings_module, "_settings_path", lambda: path)
        settings_module._cached_settings = None
        try:
            assert load_settings(use_cache=False).announcement_history == []
        finally:
            settings_module._cached_settings = None

    def test_invalid_value_falls_back_to_segmented(self, tmp_path, monkeypatch):
        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps({"transcription_provider": "bogus"}), encoding="utf-8"
        )
        monkeypatch.setattr(settings_module, "_settings_path", lambda: path)
        settings_module._cached_settings = None
        try:
            loaded = load_settings(use_cache=False)
            assert loaded.pipeline_mode == PIPELINE_MODE_SEGMENTED
        finally:
            settings_module._cached_settings = None


class TestAnnouncementFavorites:
    """The starred (favorited) announcement list persists to settings.json."""

    def test_default_is_empty(self):
        assert Settings().announcement_favorites == []

    def test_round_trip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            settings_module, "_settings_path", lambda: tmp_path / "settings.json"
        )
        settings_module._cached_settings = None
        save_settings(Settings(announcement_favorites=["Hello", "مرحبا"]))
        settings_module._cached_settings = None
        try:
            loaded = load_settings(use_cache=False)
            assert loaded.announcement_favorites == ["Hello", "مرحبا"]
        finally:
            settings_module._cached_settings = None

    def test_sanitizes_and_caps_on_load(self, tmp_path, monkeypatch):
        path = tmp_path / "settings.json"
        # Non-strings, blanks and an over-long list must be cleaned to ≤5 texts.
        path.write_text(
            json.dumps(
                {
                    "announcement_favorites": [
                        "one",
                        "  ",
                        2,
                        None,
                        "two",
                        "three",
                        "four",
                        "five",
                        "six",
                    ]
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(settings_module, "_settings_path", lambda: path)
        settings_module._cached_settings = None
        try:
            loaded = load_settings(use_cache=False)
            assert loaded.announcement_favorites == [
                "one",
                "two",
                "three",
                "four",
                "five",
            ]
        finally:
            settings_module._cached_settings = None

    def test_non_list_falls_back_to_empty(self, tmp_path, monkeypatch):
        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps({"announcement_favorites": "not a list"}), encoding="utf-8"
        )
        monkeypatch.setattr(settings_module, "_settings_path", lambda: path)
        settings_module._cached_settings = None
        try:
            assert load_settings(use_cache=False).announcement_favorites == []
        finally:
            settings_module._cached_settings = None


class TestIslamicModeSetting:
    def test_round_trip_off(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            settings_module, "_settings_path", lambda: tmp_path / "settings.json"
        )
        settings_module._cached_settings = None
        save_settings(Settings(islamic_mode=False))
        settings_module._cached_settings = None
        try:
            assert load_settings(use_cache=False).islamic_mode is False
        finally:
            settings_module._cached_settings = None

    def test_missing_key_defaults_on(self, tmp_path, monkeypatch):
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({}), encoding="utf-8")
        monkeypatch.setattr(settings_module, "_settings_path", lambda: path)
        settings_module._cached_settings = None
        try:
            assert load_settings(use_cache=False).islamic_mode is True
        finally:
            settings_module._cached_settings = None


class TestCheckForUpdatesSetting:
    def test_round_trip_off(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            settings_module, "_settings_path", lambda: tmp_path / "settings.json"
        )
        settings_module._cached_settings = None
        save_settings(Settings(check_for_updates=False))
        settings_module._cached_settings = None
        try:
            assert load_settings(use_cache=False).check_for_updates is False
        finally:
            settings_module._cached_settings = None

    def test_missing_key_defaults_on(self, tmp_path, monkeypatch):
        """Pre-existing settings files get the update check by default."""
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({}), encoding="utf-8")
        monkeypatch.setattr(settings_module, "_settings_path", lambda: path)
        settings_module._cached_settings = None
        try:
            assert load_settings(use_cache=False).check_for_updates is True
        finally:
            settings_module._cached_settings = None


class TestShowInterimTranscriptSetting:
    def test_round_trip_on(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            settings_module, "_settings_path", lambda: tmp_path / "settings.json"
        )
        settings_module._cached_settings = None
        save_settings(Settings(show_interim_transcript=True))
        settings_module._cached_settings = None
        try:
            assert load_settings(use_cache=False).show_interim_transcript is True
        finally:
            settings_module._cached_settings = None

    def test_missing_key_defaults_off(self, tmp_path, monkeypatch):
        """Settings files without the key hide the live transcript."""
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({}), encoding="utf-8")
        monkeypatch.setattr(settings_module, "_settings_path", lambda: path)
        settings_module._cached_settings = None
        try:
            assert load_settings(use_cache=False).show_interim_transcript is False
        finally:
            settings_module._cached_settings = None


class TestNoiseFilterSetting:
    def test_round_trip_off(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            settings_module, "_settings_path", lambda: tmp_path / "settings.json"
        )
        settings_module._cached_settings = None
        save_settings(Settings(noise_filter=False))
        settings_module._cached_settings = None
        try:
            assert load_settings(use_cache=False).noise_filter is False
        finally:
            settings_module._cached_settings = None

    def test_missing_key_defaults_on(self, tmp_path, monkeypatch):
        """Pre-noise-filter settings files enable the filter by default."""
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({}), encoding="utf-8")
        monkeypatch.setattr(settings_module, "_settings_path", lambda: path)
        settings_module._cached_settings = None
        try:
            assert load_settings(use_cache=False).noise_filter is True
        finally:
            settings_module._cached_settings = None


class TestAlwaysOnTopMode:
    def test_default_is_running(self):
        assert Settings().always_on_top_mode == "running"

    def test_round_trip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            settings_module, "_settings_path", lambda: tmp_path / "settings.json"
        )
        settings_module._cached_settings = None
        save_settings(Settings(always_on_top_mode="always"))
        settings_module._cached_settings = None
        try:
            assert load_settings(use_cache=False).always_on_top_mode == "always"
        finally:
            settings_module._cached_settings = None

    def test_missing_key_defaults_running(self, tmp_path, monkeypatch):
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({}), encoding="utf-8")
        monkeypatch.setattr(settings_module, "_settings_path", lambda: path)
        settings_module._cached_settings = None
        try:
            assert load_settings(use_cache=False).always_on_top_mode == "running"
        finally:
            settings_module._cached_settings = None

    def test_legacy_true_migrates_to_running(self, tmp_path, monkeypatch):
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"always_on_top": True}), encoding="utf-8")
        monkeypatch.setattr(settings_module, "_settings_path", lambda: path)
        settings_module._cached_settings = None
        try:
            assert load_settings(use_cache=False).always_on_top_mode == "running"
        finally:
            settings_module._cached_settings = None

    def test_legacy_false_migrates_to_never(self, tmp_path, monkeypatch):
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"always_on_top": False}), encoding="utf-8")
        monkeypatch.setattr(settings_module, "_settings_path", lambda: path)
        settings_module._cached_settings = None
        try:
            assert load_settings(use_cache=False).always_on_top_mode == "never"
        finally:
            settings_module._cached_settings = None


class TestRealtimeSubtitleMode:
    """Realtime is a stored, user-selectable subtitle mode (July 2026); it
    replaced the never-stored "live" override + the show_live_transcript
    setting."""

    def test_realtime_in_modes_and_default(self):
        assert SUBTITLE_MODE_REALTIME == "realtime"
        assert SUBTITLE_MODES == [
            SUBTITLE_MODE_REALTIME,
            SUBTITLE_MODE_CONTINUOUS,
            SUBTITLE_MODE_STATIC,
        ]
        assert Settings().subtitle_mode == SUBTITLE_MODE_REALTIME
        assert not hasattr(Settings(), "show_live_transcript")

    def test_round_trip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            settings_module, "_settings_path", lambda: tmp_path / "settings.json"
        )
        settings_module._cached_settings = None
        save_settings(Settings(subtitle_mode=SUBTITLE_MODE_REALTIME))
        settings_module._cached_settings = None
        try:
            loaded = load_settings(use_cache=False)
            assert loaded.subtitle_mode == SUBTITLE_MODE_REALTIME
        finally:
            settings_module._cached_settings = None

    def test_legacy_show_live_transcript_migrates_to_realtime(
        self, tmp_path, monkeypatch
    ):
        # A streaming config with the removed show_live_transcript flag was
        # showing the live feed — it lands on the Realtime mode.
        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps(
                {
                    "subtitle_mode": "continuous",
                    "transcription_provider": "deepgram",
                    "show_live_transcript": True,
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(settings_module, "_settings_path", lambda: path)
        settings_module._cached_settings = None
        try:
            loaded = load_settings(use_cache=False)
            assert loaded.subtitle_mode == SUBTITLE_MODE_REALTIME
        finally:
            settings_module._cached_settings = None

    def test_legacy_flag_ignored_for_segmented_configs(self, tmp_path, monkeypatch):
        # The flag only forced the live feed while streaming — a segmented
        # config keeps its stored mode.
        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps(
                {
                    "subtitle_mode": "static",
                    "transcription_provider": "openai",
                    "show_live_transcript": True,
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(settings_module, "_settings_path", lambda: path)
        settings_module._cached_settings = None
        try:
            loaded = load_settings(use_cache=False)
            assert loaded.subtitle_mode == SUBTITLE_MODE_STATIC
        finally:
            settings_module._cached_settings = None


class TestRetentionSettings:
    """The split retention flags and migration from the old single flag."""

    def test_fresh_install_logs_on_content_off(self, tmp_path, monkeypatch):
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({}), encoding="utf-8")
        monkeypatch.setattr(settings_module, "_settings_path", lambda: path)
        settings_module._cached_settings = None
        try:
            loaded = load_settings(use_cache=False)
            assert loaded.auto_cleanup_logs is True
            assert loaded.auto_cleanup_content is False
        finally:
            settings_module._cached_settings = None

    def test_legacy_auto_cleanup_true_preserves_content_deletion(
        self, tmp_path, monkeypatch
    ):
        # An existing user with the old flag on was already deleting history —
        # migrate to both on so their behaviour is unchanged.
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"auto_cleanup": True}), encoding="utf-8")
        monkeypatch.setattr(settings_module, "_settings_path", lambda: path)
        settings_module._cached_settings = None
        try:
            loaded = load_settings(use_cache=False)
            assert loaded.auto_cleanup_logs is True
            assert loaded.auto_cleanup_content is True
        finally:
            settings_module._cached_settings = None

    def test_legacy_auto_cleanup_false_migrates_both_off(self, tmp_path, monkeypatch):
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"auto_cleanup": False}), encoding="utf-8")
        monkeypatch.setattr(settings_module, "_settings_path", lambda: path)
        settings_module._cached_settings = None
        try:
            loaded = load_settings(use_cache=False)
            assert loaded.auto_cleanup_logs is False
            assert loaded.auto_cleanup_content is False
        finally:
            settings_module._cached_settings = None


class TestLanguageEndonyms:
    """Dropdowns show the native endonym; the English name stays the canonical
    key for settings storage, language codes, footer lookups and the
    translation/summary prompts. Every dropdown must therefore convert on the
    way in and on the way back out.
    """

    def test_every_target_language_round_trips(self):
        for name in TARGET_LANGUAGE_NAMES:
            shown = language_display_name(name)
            assert language_canonical_name(shown) == name, (
                f"{name!r} displays as {shown!r} but does not map back"
            )

    def test_every_source_language_round_trips(self):
        for name, _code in SOURCE_LANGUAGES:
            shown = language_display_name(name)
            assert language_canonical_name(shown) == name

    def test_canonical_names_pass_through_unchanged(self):
        """A settings.json written before endonyms existed stores English
        names — those must keep resolving."""
        for legacy in ("German", "Arabic", "Turkish", "English"):
            assert language_canonical_name(legacy) == legacy

    def test_unknown_value_is_returned_as_is(self):
        assert language_display_name("Klingon") == "Klingon"
        assert language_canonical_name("Klingon") == "Klingon"

    def test_automatic_is_not_translated(self):
        """"Automatic" is a mode, not a language, so it keeps its label."""
        assert language_display_name("Automatic") == "Automatic"
        assert language_canonical_name("Automatic") == "Automatic"

    def test_known_endonyms(self):
        assert language_display_name("German") == "Deutsch"
        assert language_display_name("Arabic") == "العربية"
        assert language_display_name("Turkish") == "Türkçe"

    def test_canonical_names_still_resolve_to_language_codes(self):
        """The whole point of keeping English canonical: the code lookup and
        every other keyed table must not see an endonym."""
        assert get_target_language_code(language_canonical_name("Deutsch")) == "de"
        assert get_source_language_code(language_canonical_name("العربية")) == "ar"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
