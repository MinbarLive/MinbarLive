"""GUI translation files must cover the onboarding-wizard keys."""

import json
import os
import sys
from pathlib import Path

import pytest

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import GUI_TRANSLATIONS_DIR
from utils.settings import GUI_LANGUAGE_CODES

WIZARD_KEYS = [
    "ai_provider",
    "dlg_paste_key_any",
    "dlg_enter_key_any",
    "wizard_title",
    "wizard_step_of",
    "wizard_back",
    "wizard_next",
    "wizard_finish",
    "wizard_ui_language_title",
    "wizard_ui_language_sub",
    "wizard_languages_title",
    "wizard_source_language",
    "wizard_target_language",
    "wizard_audio_title",
    "wizard_audio_sub",
    "wizard_no_devices",
    "wizard_provider_title",
    "wizard_provider",
    "wizard_api_key",
    "wizard_keys_info",
    "wizard_gemini_rag_note",
    "wizard_anthropic_stt_note",
    "wizard_show_key",
    "wizard_key_saved_hint",
    "wizard_disclaimer_title",
    "wizard_disclaimer_text",
    "wizard_disclaimer_accept",
    # Control-panel keys added alongside wizard features
    "bilingual_mode",
    "show_interim_transcript",
    "input_level",
    "input_level_no_signal",
    "input_level_test",
    "input_level_stop_test",
    "input_level_clipping",
    "subtitle_output_enabled",
    "batch_file",
    "batch_file_sub",
    "batch_pick_file",
    "batch_no_file",
    "batch_media_files",
    "batch_all_files",
    "batch_start",
    "batch_cancel",
    "batch_progress",
    "batch_done",
    "batch_transcription_model",
    "batch_translation_model",
    "batch_open_history",
    "batch_open_folder",
    "batch_source_language",
    "batch_target_language",
    "batch_cancelled",
    "batch_error",
    "batch_ffmpeg_missing",
    "batch_ffmpeg_download_prompt",
    "batch_ffmpeg_downloading",
    "batch_more_settings",
    "batch_defaults",
    "batch_output_format",
    "batch_output_srt",
    "batch_output_text",
    "batch_output_both",
    "batch_bilingual_srt",
    "noise_filter",
    "always_on_top",
    "islamic_mode",
    "islamic_mode_enabled",
    "islamic_mode_hint",
    "islamic_mode_off_confirm",
    "history_title",
    "history_tab_sessions",
    "history_tab_logs",
    "history_tab_batch",
    "history_tab_cost",
    "cost_empty",
    "cost_estimate_note",
    "cost_unpriced",
    "cost_requests",
    "cost_last_30_days",
    "history_empty",
    "history_batch_empty",
    "history_entries",
    "history_minutes",
    "history_seconds",
    "history_export",
    "history_copy",
    "history_copied",
    "history_delete",
    "history_delete_confirm",
    "history_summarise",
    "summary_title",
    "summary_provider",
    "summary_language",
    "summary_generate",
    "summary_generating",
    "summary_copy",
    "summary_copied",
    "summary_save",
    "summary_saved",
    "summary_loaded",
    "summary_failed",
    "subtitle_mode_realtime",
    "wizard_theme_label",
    "wizard_key_help",
    "theme_light",
    "theme_dark",
    "translation_provider",
    "transcription_provider",
    "section_transcription",
    "section_translation",
    "strategy_realtime",
    "strategy_chunk",
    "strategy_semantic",
    "api_key_status_saved",
    "api_key_status_none",
    "api_key_select_provider",
    "update_available",
    "check_updates_on_launch",
    # Settings window sections
    "settings_general",
    "settings_appearance",
    "updates_section",
    # Subtitle appearance (control-panel expander)
    "subtitle_appearance",
    "source_text_size",
    "translation_text_color",
    "source_text_color",
    "color_default",
    "color_choose",
    # Announcement (megaphone) window
    "announce_title",
    "announce_sub",
    "announce_duration_label",
    "announce_duration_10s",
    "announce_duration_30s",
    "announce_duration_1m",
    "announce_duration_5m",
    "announce_duration_until_stopped",
    "announce_favorites",
    "announce_favorites_full",
    "announce_recent",
    "announce_recent_empty",
    "announce_send",
    "announce_stop",
    "announce_stop_on_live_stop",
]


def _load(code: str) -> dict:
    path = os.path.join(GUI_TRANSLATIONS_DIR, f"{code}.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class TestWizardTranslationCoverage:
    @pytest.mark.parametrize("code", GUI_LANGUAGE_CODES)
    def test_all_wizard_keys_present(self, code):
        data = _load(code)
        for key in WIZARD_KEYS:
            assert key in data, f"{code}.json missing '{key}'"
            assert data[key].strip(), f"{code}.json has empty '{key}'"

    @pytest.mark.parametrize("code", GUI_LANGUAGE_CODES)
    def test_format_placeholders_survive_translation(self, code):
        data = _load(code)
        assert "{current}" in data["wizard_step_of"]
        assert "{total}" in data["wizard_step_of"]
        assert "{provider}" in data["dlg_paste_key_any"]
        assert "{provider}" in data["dlg_enter_key_any"]
        assert "{current}" in data["batch_progress"]
        assert "{total}" in data["batch_progress"]
        assert "{name}" in data["batch_done"]
        assert "{error}" in data["batch_error"]
        assert "{mb}" in data["batch_ffmpeg_download_prompt"]
        assert "{percent}" in data["batch_ffmpeg_downloading"]
        assert "{count}" in data["history_entries"]
        assert "{minutes}" in data["history_minutes"]
        assert "{seconds}" in data["history_seconds"]
        assert "{error}" in data["summary_failed"]
        assert "{version}" in data["update_available"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
