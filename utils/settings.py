"""User-configurable runtime settings stored in user profile.

This module handles MUTABLE user preferences (API key, language, monitor index,
font size) that persist across sessions in the user's AppData directory.

For static technical constants (audio params, model names, thresholds),
see config.py instead.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from utils.app_paths import get_app_data_dir

SETTINGS_FILENAME = "settings.json"

# Supported source languages (spoken language in the mosque)
# These map to ISO 639-1 codes for transcription
SOURCE_LANGUAGES = [
    ("Automatic", None),  # Let the model auto-detect
    ("Arabic", "ar"),
    ("German", "de"),
    ("English", "en"),
    ("Turkish", "tr"),
    ("Urdu", "ur"),
    ("Indonesian", "id"),
    ("Malay", "ms"),
    ("Persian (Farsi)", "fa"),
    ("Bengali", "bn"),
    ("Pashto", "ps"),
    ("Somali", "so"),
    ("Swahili", "sw"),
    ("Hausa", "ha"),
    ("Kurdish", "ku"),
    ("Bosnian", "bs"),
    ("Albanian", "sq"),
]

# Supported target languages for translation with ISO codes
# Format: (display_name, iso_code)
TARGET_LANGUAGES = [
    ("German", "de"),
    ("English", "en"),
    ("Arabic", "ar"),
    ("Turkish", "tr"),
    ("Albanian", "sq"),
    ("Bengali", "bn"),
    ("Bosnian", "bs"),
    ("Chinese (Simplified)", "zh-hans"),
    ("Chinese (Traditional)", "zh-hant"),
    ("Dutch", "nl"),
    ("French", "fr"),
    ("Hausa", "ha"),
    ("Hindi", "hi"),
    ("Indonesian", "id"),
    ("Italian", "it"),
    ("Japanese", "ja"),
    ("Korean", "ko"),
    ("Kurdish", "ku"),
    ("Malay", "ms"),
    ("Pashto", "ps"),
    ("Persian (Farsi)", "fa"),
    ("Polish", "pl"),
    ("Portuguese", "pt"),
    ("Punjabi", "pa"),
    ("Russian", "ru"),
    ("Sindhi", "sd"),
    ("Somali", "so"),
    ("Spanish", "es"),
    ("Swahili", "sw"),
    ("Swedish", "sv"),
    ("Tagalog", "tl"),
    ("Tamil", "ta"),
    ("Thai", "th"),
    ("Urdu", "ur"),
    ("Vietnamese", "vi"),
]

# Helper to get target language names (for GUI dropdowns)
TARGET_LANGUAGE_NAMES = [name for name, _ in TARGET_LANGUAGES]

# Native names (endonyms) shown in the source/target dropdowns. The English
# name above stays the CANONICAL key everywhere else — settings storage,
# footer_translations.json lookups, language codes and the translation prompt
# all key off it — so only the dropdown DISPLAY uses the endonym. Keys must
# match the names in SOURCE_LANGUAGES / TARGET_LANGUAGES. "Automatic" is a mode,
# not a language, so it keeps its label.
LANGUAGE_ENDONYMS = {
    "Automatic": "Automatic",
    "Arabic": "العربية",
    "German": "Deutsch",
    "English": "English",
    "Turkish": "Türkçe",
    "Urdu": "اردو",
    "Indonesian": "Bahasa Indonesia",
    "Malay": "Bahasa Melayu",
    "Persian (Farsi)": "فارسی",
    "Bengali": "বাংলা",
    "Pashto": "پښتو",
    "Somali": "Soomaali",
    "Swahili": "Kiswahili",
    "Hausa": "Hausa",
    "Kurdish": "Kurdî",
    "Bosnian": "Bosanski",
    "Albanian": "Shqip",
    "Chinese (Simplified)": "简体中文",
    "Chinese (Traditional)": "繁體中文",
    "Dutch": "Nederlands",
    "French": "Français",
    "Hindi": "हिन्दी",
    "Italian": "Italiano",
    "Japanese": "日本語",
    "Korean": "한국어",
    "Polish": "Polski",
    "Portuguese": "Português",
    "Punjabi": "ਪੰਜਾਬੀ",
    "Russian": "Русский",
    "Sindhi": "سنڌي",
    "Spanish": "Español",
    "Swedish": "Svenska",
    "Tagalog": "Tagalog",
    "Tamil": "தமிழ்",
    "Thai": "ไทย",
    "Vietnamese": "Tiếng Việt",
}

# Reverse map (endonym -> English canonical) plus identity for the canonical
# names themselves, so a stored/legacy English value round-trips unchanged.
_ENDONYM_TO_CANONICAL = {endo: canon for canon, endo in LANGUAGE_ENDONYMS.items()}
for _canon in LANGUAGE_ENDONYMS:
    _ENDONYM_TO_CANONICAL.setdefault(_canon, _canon)


def language_display_name(canonical: str) -> str:
    """English canonical name -> native endonym shown in the dropdowns."""
    return LANGUAGE_ENDONYMS.get(canonical, canonical)


def language_canonical_name(display: str) -> str:
    """Native endonym (what a dropdown returns) -> English canonical name used
    everywhere else. Passing an already-canonical name returns it unchanged."""
    return _ENDONYM_TO_CANONICAL.get(display, display)


# Endonym display list in the same order as the canonical list. (The source
# dropdowns filter their entries per pipeline mode, so they map
# language_display_name() over their own filtered list instead.)
TARGET_LANGUAGE_DISPLAY_NAMES = [
    language_display_name(name) for name in TARGET_LANGUAGE_NAMES
]

# Available translation providers (see providers/ package). Only registered
# providers belong here; unknown values in settings.json fall back to the
# default. This drives the ``ai_provider`` setting (translation only).
AI_PROVIDERS = ["gemini", "openai", "anthropic"]
DEFAULT_AI_PROVIDER = "gemini"

# Available transcription providers. "openai"/"gemini" run the segmented
# pipeline; the "*_realtime" ids and "deepgram" are real-time streaming
# engines (pipeline_mode is derived from this). "openai_realtime" /
# "gemini_realtime" are different engines from segmented "openai"/"gemini"
# but use the same API keys. Kept separate from AI_PROVIDERS so the
# translation LLM and the speech-to-text engine can be chosen independently.
TRANSCRIPTION_PROVIDERS = [
    "gemini",
    "openai",
    "deepgram",
    "gemini_realtime",
    "openai_realtime",
]
# Fresh installs default to real-time streaming on Gemini — one key covers
# translation, transcription and RAG (the Gemini embedding space ships with
# the app). Invalid stored values fall back to the segmented default below
# instead.
DEFAULT_TRANSCRIPTION_PROVIDER = "gemini_realtime"
DEFAULT_SEGMENTED_TRANSCRIPTION_PROVIDER = "gemini"
STREAMING_TRANSCRIPTION_PROVIDERS = ["gemini_realtime", "openai_realtime", "deepgram"]
DEFAULT_STREAMING_TRANSCRIPTION_PROVIDER = "gemini_realtime"

# Available translation models (display_name, model_id)
# Keep this list focused on practical TEXT translation models.
# Excludes image/audio/realtime/search/codex variants and legacy deprecated options.
# Organized by speed/cost: fastest first, highest quality last.
TRANSLATION_MODELS = [
    # Quality tier (gpt-5.5 live-probed 2026-07-15: 3.9s Arabic→German)
    ("GPT-5.5 (Highest Quality)", "gpt-5.5"),
    ("GPT-5.4", "gpt-5.4"),
    ("GPT-5.2 (Stable High Quality)", "gpt-5.2"),
    # Balanced tier
    ("GPT-5.1", "gpt-5.1"),
    ("GPT-5", "gpt-5"),
    ("GPT-4.1", "gpt-4.1"),
    ("GPT-4o", "gpt-4o"),
    # Real-time tier (low latency; 5.4-mini/nano probed 1.2s/1.0s)
    ("GPT-5.4 Mini", "gpt-5.4-mini"),
    ("GPT-5.4 Nano", "gpt-5.4-nano"),
    ("GPT-5 Mini", "gpt-5-mini"),
    ("GPT-5 Nano", "gpt-5-nano"),
    ("GPT-4.1 Mini", "gpt-4.1-mini"),
    ("GPT-4o Mini", "gpt-4o-mini"),
]

# Default model
DEFAULT_TRANSLATION_MODEL = "gpt-5.2"

# Fallback models to try if primary model fails (in order)
# These use the same OpenAI API, but different models may have different availability
FALLBACK_TRANSLATION_MODELS = [
    "gpt-5.2",
    "gpt-5.1",
    "gpt-4.1",
    "gpt-4o-mini",
]

# Available transcription models (display_name, model_id)
TRANSCRIPTION_MODELS = [
    ("GPT-4o Transcribe (Recommended)", "gpt-4o-transcribe"),
    ("GPT-4o Mini Transcribe (Faster & Cheaper)", "gpt-4o-mini-transcribe"),
]

# Default transcription model
DEFAULT_TRANSCRIPTION_MODEL = "gpt-4o-transcribe"

# Fallback transcription models to try if primary model fails (in order)
FALLBACK_TRANSCRIPTION_MODELS = [
    "gpt-4o-transcribe",
    "gpt-4o-mini-transcribe",
    "whisper-1",  # Legacy Whisper model as last resort
]


# Helper to get language code from name
def get_source_language_code(name: str) -> str | None:
    for lang_name, code in SOURCE_LANGUAGES:
        if lang_name == name:
            return code
    return None


def get_target_language_code(name: str) -> str | None:
    """Get ISO language code for a target language name."""
    for lang_name, code in TARGET_LANGUAGES:
        if lang_name == name:
            return code
    return None


# Subtitle display modes ("stack" was removed July 2026; stored "stack"
# values fall back to continuous via the validation in load_settings)
SUBTITLE_MODE_CONTINUOUS = "continuous"  # Continuous upward scroll animation
SUBTITLE_MODE_STATIC = "static"  # Only show the most recent subtitle
# Realtime feed (the default): top-down feed with the in-progress transcript
# line — settled translations stack from the top, the live text writes below
# them (Baian-style). Streaming-only: under a segmented strategy the GUI
# falls back to continuous and the mode returns when streaming is
# re-selected. Replaces the never-stored "live" override mode + the
# show_live_transcript setting (removed July 2026, migrated below).
SUBTITLE_MODE_REALTIME = "realtime"
SUBTITLE_MODES = [
    SUBTITLE_MODE_REALTIME,
    SUBTITLE_MODE_CONTINUOUS,
    SUBTITLE_MODE_STATIC,
]

# Pipeline modes: "segmented" is the default fixed-length-segment pipeline
# above (chunk/semantic buffering both apply); "streaming" is the opt-in
# real-time path (Deepgram Nova-3) that bypasses buffering entirely and
# flushes on the provider's own utterance-end signal instead (see P7 in
# CLAUDE.md). Orthogonal to ai_provider: streaming only replaces
# transcription, translation still uses the configured ai_provider.
PIPELINE_MODE_SEGMENTED = "segmented"
PIPELINE_MODE_STREAMING = "streaming"
PIPELINE_MODES = [PIPELINE_MODE_SEGMENTED, PIPELINE_MODE_STREAMING]


# Supported GUI languages (code, display_name)
GUI_LANGUAGES = [
    ("de", "Deutsch"),
    ("en", "English"),
    ("ar", "العربية"),
    ("bs", "Bosanski"),
    ("sq", "Shqip"),
    ("tr", "Türkçe"),
]
GUI_LANGUAGE_CODES = [code for code, _ in GUI_LANGUAGES]
DEFAULT_GUI_LANGUAGE = "de"
THEME_MODES = ["dark", "light"]
DEFAULT_THEME_MODE = "light"

_HEX_COLOR_RE = re.compile(r"#[0-9A-Fa-f]{6}")


def _finite_float(value: object) -> float | None:
    """Convert a real numeric JSON value without accepting bool/NaN/overflow."""

    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _load_source_font_size_base(value: object, font_size_base: object) -> float:
    """Return a safe source-text font divisor for old and new settings files.

    Before source and translation typography were configurable independently,
    source text rendered at 70% of the translation size.  Because these values
    are *divisors*, preserving that appearance means ``font_size_base / 0.7``.
    """

    translation_base = _finite_float(font_size_base)
    fallback = (translation_base if translation_base is not None else 40.0) / 0.7
    source_base = _finite_float(value)
    if source_base is None:
        source_base = fallback
    return max(20.0, min(120.0, source_base))


def _load_subtitle_text_color(value: object) -> str:
    """Accept only an exact CSS-style ``#RRGGBB`` subtitle override."""

    if isinstance(value, str) and _HEX_COLOR_RE.fullmatch(value):
        return value
    return ""


@dataclass
class Settings:
    # Note: openai_api_key is stored securely via keyring, not in this dataclass
    monitor_index: int = 1
    # False keeps transcription/translation running without ever creating the
    # separate audience overlay. ``monitor_index`` still remembers the last
    # real monitor so re-enabling output restores the previous destination.
    subtitle_output_enabled: bool = True
    input_device_name: str | None = None
    font_size_base: int = 40
    # Separate source/original-text divisor.  The default exactly preserves
    # the historical 70%-of-translation size (larger divisor = smaller text).
    source_font_size_base: float = 40 / 0.7
    # Empty means use the active subtitle theme's normal text-role color.
    translation_text_color: str = ""
    source_text_color: str = ""
    source_language: str = "Automatic"
    target_language: str = "German"
    subtitle_mode: str = SUBTITLE_MODE_REALTIME  # realtime, continuous, or static
    scroll_speed: float = 1.0  # Scroll speed for continuous mode (0.5 to 5.0)
    transparent_static: bool = False  # Transparent background for static mode
    window_height_percent: int = 50  # Window height as % of screen (5-100)
    translation_model: str = DEFAULT_TRANSLATION_MODEL  # OpenAI model for translation
    transcription_model: str = (
        DEFAULT_TRANSCRIPTION_MODEL  # OpenAI model for transcription
    )
    use_default_translation_model: bool = True  # Use default translation model
    use_default_transcription_model: bool = True  # Use default transcription model
    processing_strategy: str = "chunk"  # "chunk" or "semantic"
    use_default_processing_strategy: bool = True  # Use default processing strategy
    gui_language: str = DEFAULT_GUI_LANGUAGE  # GUI language (de, en)
    theme_mode: str = DEFAULT_THEME_MODE  # Control-panel theme (dark or light)
    subtitle_theme_mode: str = (
        DEFAULT_THEME_MODE  # Subtitle-window theme (dark or light)
    )
    show_footer: bool = True  # Show footer disclaimer in subtitle window
    # Show original text above the translation (default ON since 2026-07-15,
    # together with the live transcript line — user decision)
    bilingual_mode: bool = True
    # Realtime mode only: show the in-progress transcript ("live line") while
    # the speaker is still talking. Off = the feed shows only finished
    # translation blocks as they land.
    show_interim_transcript: bool = True
    # Islamic mode (default on): Quran verse RAG matching + verified-verse
    # bypass + Athan detection + Islamic translation prompt. Off = general
    # professional translator for non-religious content (safety-locked in
    # the GUI so it can't be switched off accidentally).
    islamic_mode: bool = True
    hide_subtitle_on_stop: bool = False  # Hide subtitle window when stopped
    # Keep the subtitle overlay (and, while it is open, the control panel)
    # above other windows. Off = neither window is ever topmost; the control
    # panel is also never topmost while no subtitle overlay is open.
    always_on_top: bool = True
    # Voice-activity noise filter (audio/vad.py): skip/zero-fill non-speech
    # audio (static, hum) that the loudness-based silence gate lets through.
    noise_filter: bool = True
    adaptive_subtitle_catchup: bool = True  # Speed up display when backlog grows
    # Retention is split: logs are pure diagnostics (auto-purge on), while
    # history transcripts + batch SRT/TXT are the user's own content (opt-in).
    auto_cleanup_logs: bool = True  # Purge old log files at startup
    auto_cleanup_content: bool = False  # Purge old history + batch files at startup
    log_panel_collapsed: bool = True  # Log panel hidden by default (AV volunteers)
    window_geometry: str = ""  # Last window geometry (WxH+X+Y), empty = use default
    auto_start: bool = False  # Start translation automatically when app launches
    # Stop a running session automatically after 10 min without any
    # transcription (AUTO_STOP_INACTIVITY_SECONDS) — cost guard for
    # forgotten sessions, esp. streaming's per-minute billing.
    auto_stop_inactivity: bool = True
    # Startup update check: one anonymous GET to the GitHub releases API;
    # a newer release shows a dismissible notice in the control panel.
    check_for_updates: bool = True
    ai_provider: str = DEFAULT_AI_PROVIDER  # Translation provider (providers/ pkg)
    transcription_provider: str = (
        DEFAULT_TRANSCRIPTION_PROVIDER  # STT engine (streaming ones => streaming)
    )
    onboarding_completed: bool = False  # First-run setup wizard finished
    disclaimer_accepted: bool = False  # AI-translation disclaimer acknowledged
    # Derived from transcription_provider; streaming by default to match the
    # deepgram default above (fresh installs open in real-time mode).
    pipeline_mode: str = PIPELINE_MODE_STREAMING
    # Last choices in the history "Summarise session" dialog (empty => fall
    # back to ai_provider / target_language at open time).
    last_summary_provider: str = ""
    last_summary_language: str = ""
    # Recent announcement (megaphone) texts, most-recent-first, for quick
    # re-use in the announcement window. Capped at ANNOUNCEMENT_HISTORY_MAX.
    announcement_history: list[str] = field(default_factory=list)
    # Starred announcement texts, most-recent-first — excluded from the
    # rotating history above so they never get evicted by newer sends.
    # Capped at ANNOUNCEMENT_FAVORITES_MAX.
    announcement_favorites: list[str] = field(default_factory=list)
    # Last-selected announcement duration (index into
    # ANNOUNCEMENT_DURATIONS_SECONDS); remembered across restarts. Default 30s.
    announcement_duration_index: int = 1


def _settings_path() -> Path:
    return get_app_data_dir() / SETTINGS_FILENAME


# In-memory cache to avoid repeated disk reads during translation
_cached_settings: Settings | None = None


def load_settings(use_cache: bool = True) -> Settings:
    """
    Load settings from disk.

    Args:
        use_cache: If True, return cached settings if available.
                   Set to False to force a fresh read from disk.

    Returns:
        The current settings.
    """
    global _cached_settings

    if use_cache and _cached_settings is not None:
        return _cached_settings

    path = _settings_path()
    if not path.exists():
        _cached_settings = Settings()
        return _cached_settings

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        # Migrate old scrolling_subtitles boolean to new subtitle_mode
        # (scrolling used to map to the removed "stack" mode; continuous is
        # the closest surviving equivalent)
        subtitle_mode = data.get("subtitle_mode", None)
        if subtitle_mode is None:
            # Check for old boolean setting
            old_scrolling = data.get("scrolling_subtitles", False)
            subtitle_mode = (
                SUBTITLE_MODE_CONTINUOUS if old_scrolling else SUBTITLE_MODE_STATIC
            )
        # Validate mode
        if subtitle_mode not in SUBTITLE_MODES:
            subtitle_mode = SUBTITLE_MODE_CONTINUOUS
        theme_mode = data.get("theme_mode", DEFAULT_THEME_MODE)
        if theme_mode not in THEME_MODES:
            theme_mode = DEFAULT_THEME_MODE
        subtitle_theme_mode = data.get("subtitle_theme_mode", DEFAULT_THEME_MODE)
        if subtitle_theme_mode not in THEME_MODES:
            subtitle_theme_mode = DEFAULT_THEME_MODE
        ai_provider = data.get("ai_provider", DEFAULT_AI_PROVIDER)
        if ai_provider not in AI_PROVIDERS:
            ai_provider = DEFAULT_AI_PROVIDER
        # transcription_provider is the single source of truth for the STT
        # engine AND, by extension, the pipeline mode (streaming engines =>
        # streaming).
        transcription_provider = data.get("transcription_provider")
        if transcription_provider is None:
            # Legacy settings predate the translation/transcription split —
            # infer from the fields that existed then. Streaming configs from
            # that era were Deepgram sessions (the only engine then), so they
            # keep their engine rather than getting the current default.
            if data.get("pipeline_mode") == PIPELINE_MODE_STREAMING:
                transcription_provider = "deepgram"
            elif ai_provider in ("openai", "gemini"):
                # Pre-split, transcription used the ai_provider directly.
                transcription_provider = ai_provider
            else:
                # Legacy file (e.g. anthropic) with no transcription engine —
                # those sessions transcribed via the OpenAI fallback, so keep
                # that (literal: the app default moved to Gemini later).
                transcription_provider = "openai"
        if transcription_provider not in TRANSCRIPTION_PROVIDERS:
            transcription_provider = DEFAULT_SEGMENTED_TRANSCRIPTION_PROVIDER
        pipeline_mode = (
            PIPELINE_MODE_STREAMING
            if transcription_provider in STREAMING_TRANSCRIPTION_PROVIDERS
            else PIPELINE_MODE_SEGMENTED
        )
        # Legacy show_live_transcript (removed July 2026): while streaming it
        # forced the live-feed display — those users land on the Realtime
        # subtitle mode that replaced it.
        if (
            data.get("show_live_transcript", False)
            and pipeline_mode == PIPELINE_MODE_STREAMING
        ):
            subtitle_mode = SUBTITLE_MODE_REALTIME
        announcement_history = data.get("announcement_history", [])
        if not isinstance(announcement_history, list):
            announcement_history = []
        announcement_history = [
            t for t in announcement_history if isinstance(t, str) and t.strip()
        ][:3]
        announcement_favorites = data.get("announcement_favorites", [])
        if not isinstance(announcement_favorites, list):
            announcement_favorites = []
        announcement_favorites = [
            t for t in announcement_favorites if isinstance(t, str) and t.strip()
        ][:5]
        announcement_duration_index = data.get("announcement_duration_index", 1)
        if not isinstance(announcement_duration_index, int):
            announcement_duration_index = 1
        font_size_base = data.get("font_size_base", 40)
        source_font_size_base = _load_source_font_size_base(
            data.get("source_font_size_base"), font_size_base
        )
        translation_text_color = _load_subtitle_text_color(
            data.get("translation_text_color", "")
        )
        source_text_color = _load_subtitle_text_color(
            data.get("source_text_color", "")
        )
        subtitle_output_enabled = data.get("subtitle_output_enabled", True)
        if not isinstance(subtitle_output_enabled, bool):
            subtitle_output_enabled = True
        _cached_settings = Settings(
            monitor_index=data.get("monitor_index", 1),
            subtitle_output_enabled=subtitle_output_enabled,
            input_device_name=data.get("input_device_name"),
            font_size_base=font_size_base,
            source_font_size_base=source_font_size_base,
            translation_text_color=translation_text_color,
            source_text_color=source_text_color,
            source_language=data.get("source_language", "Automatic"),
            target_language=data.get("target_language", "German"),
            subtitle_mode=subtitle_mode,
            scroll_speed=data.get("scroll_speed", 1.0),
            transparent_static=data.get("transparent_static", False),
            window_height_percent=max(
                5, min(100, data.get("window_height_percent", 50))
            ),
            translation_model=data.get("translation_model", DEFAULT_TRANSLATION_MODEL),
            transcription_model=data.get(
                "transcription_model", DEFAULT_TRANSCRIPTION_MODEL
            ),
            use_default_translation_model=data.get(
                "use_default_translation_model", True
            ),
            use_default_transcription_model=data.get(
                "use_default_transcription_model", True
            ),
            processing_strategy=data.get("processing_strategy", "chunk"),
            use_default_processing_strategy=data.get(
                "use_default_processing_strategy", True
            ),
            gui_language=data.get("gui_language", DEFAULT_GUI_LANGUAGE),
            theme_mode=theme_mode,
            subtitle_theme_mode=subtitle_theme_mode,
            show_footer=data.get("show_footer", True),
            bilingual_mode=data.get("bilingual_mode", True),
            show_interim_transcript=data.get("show_interim_transcript", True),
            islamic_mode=data.get("islamic_mode", True),
            hide_subtitle_on_stop=data.get("hide_subtitle_on_stop", False),
            always_on_top=data.get("always_on_top", True),
            noise_filter=data.get("noise_filter", True),
            adaptive_subtitle_catchup=data.get("adaptive_subtitle_catchup", True),
            # Migrate the old single flag: an existing user who had cleanup on
            # was already deleting history, so preserve that (content=on);
            # fresh installs get the new default (logs on, content opt-in).
            auto_cleanup_logs=data.get(
                "auto_cleanup_logs", data.get("auto_cleanup", True)
            ),
            auto_cleanup_content=data.get(
                "auto_cleanup_content", data.get("auto_cleanup", False)
            ),
            log_panel_collapsed=data.get("log_panel_collapsed", True),
            window_geometry=data.get("window_geometry", ""),
            auto_start=data.get("auto_start", False),
            auto_stop_inactivity=data.get("auto_stop_inactivity", True),
            check_for_updates=data.get("check_for_updates", True),
            ai_provider=ai_provider,
            transcription_provider=transcription_provider,
            onboarding_completed=data.get("onboarding_completed", False),
            disclaimer_accepted=data.get("disclaimer_accepted", False),
            pipeline_mode=pipeline_mode,
            last_summary_provider=data.get("last_summary_provider", ""),
            last_summary_language=data.get("last_summary_language", ""),
            announcement_history=announcement_history,
            announcement_favorites=announcement_favorites,
            announcement_duration_index=announcement_duration_index,
        )
        return _cached_settings
    except Exception:
        # If corrupted, fail safe: treat as empty.
        _cached_settings = Settings()
        return _cached_settings


def save_settings(settings: Settings) -> None:
    """Save settings to disk and update the cache."""
    global _cached_settings

    dir_path = _settings_path().parent
    dir_path.mkdir(parents=True, exist_ok=True)

    # Note: API key is stored securely via keyring, not in this file
    payload = {
        "monitor_index": settings.monitor_index,
        "subtitle_output_enabled": settings.subtitle_output_enabled,
        "input_device_name": settings.input_device_name,
        "font_size_base": settings.font_size_base,
        "source_font_size_base": settings.source_font_size_base,
        "translation_text_color": settings.translation_text_color,
        "source_text_color": settings.source_text_color,
        "source_language": settings.source_language,
        "target_language": settings.target_language,
        "subtitle_mode": settings.subtitle_mode,
        "scroll_speed": settings.scroll_speed,
        "transparent_static": settings.transparent_static,
        "window_height_percent": settings.window_height_percent,
        "translation_model": settings.translation_model,
        "transcription_model": settings.transcription_model,
        "use_default_translation_model": settings.use_default_translation_model,
        "use_default_transcription_model": settings.use_default_transcription_model,
        "processing_strategy": settings.processing_strategy,
        "use_default_processing_strategy": settings.use_default_processing_strategy,
        "gui_language": settings.gui_language,
        "theme_mode": settings.theme_mode,
        "subtitle_theme_mode": settings.subtitle_theme_mode,
        "show_footer": settings.show_footer,
        "bilingual_mode": settings.bilingual_mode,
        "show_interim_transcript": settings.show_interim_transcript,
        "islamic_mode": settings.islamic_mode,
        "hide_subtitle_on_stop": settings.hide_subtitle_on_stop,
        "always_on_top": settings.always_on_top,
        "noise_filter": settings.noise_filter,
        "adaptive_subtitle_catchup": settings.adaptive_subtitle_catchup,
        "auto_cleanup_logs": settings.auto_cleanup_logs,
        "auto_cleanup_content": settings.auto_cleanup_content,
        "log_panel_collapsed": settings.log_panel_collapsed,
        "window_geometry": settings.window_geometry,
        "auto_start": settings.auto_start,
        "auto_stop_inactivity": settings.auto_stop_inactivity,
        "check_for_updates": settings.check_for_updates,
        "ai_provider": settings.ai_provider,
        "transcription_provider": settings.transcription_provider,
        "onboarding_completed": settings.onboarding_completed,
        "disclaimer_accepted": settings.disclaimer_accepted,
        "pipeline_mode": settings.pipeline_mode,
        "last_summary_provider": settings.last_summary_provider,
        "last_summary_language": settings.last_summary_language,
        "announcement_history": settings.announcement_history,
        "announcement_favorites": settings.announcement_favorites,
        "announcement_duration_index": settings.announcement_duration_index,
    }
    tmp = _settings_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_settings_path())

    # Update the cache
    _cached_settings = settings


def get_saved_api_key() -> str | None:
    """Get the API key from secure storage (keyring) or legacy settings."""
    from utils.keyring_storage import get_api_key_from_keyring, is_keyring_available

    # Try keyring first (secure storage)
    if is_keyring_available():
        key = get_api_key_from_keyring()
        if key:
            return key

    # Fallback: check for legacy key in settings file and migrate it
    path = _settings_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            legacy_key = data.get("openai_api_key")
            if legacy_key:
                # Migrate to secure storage
                set_saved_api_key(legacy_key)
                # Remove from settings file
                _remove_legacy_api_key_from_file()
                return legacy_key
        except Exception:
            pass

    return None


def _remove_legacy_api_key_from_file() -> None:
    """Remove legacy API key from settings.json after migration to keyring."""
    path = _settings_path()
    if not path.exists():
        return

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if "openai_api_key" in data:
            del data["openai_api_key"]
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(path)
    except Exception:
        pass  # Best effort cleanup


def set_saved_api_key(key: str) -> bool:
    """Save the API key to secure storage (keyring).

    Returns:
        True if stored securely, False if fell back to settings file.
    """
    from utils.keyring_storage import is_keyring_available, set_api_key_in_keyring

    key = (key or "").strip()
    if not key:
        return False

    if is_keyring_available():
        if set_api_key_in_keyring(key):
            return True

    # Fallback: store in settings file (with warning logged in keyring_storage)
    path = _settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            data = {}
        data["openai_api_key"] = key
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        return False  # Stored but not securely
    except Exception:
        return False


def delete_saved_api_key() -> None:
    """Delete the API key from secure storage and any legacy location."""
    from utils.keyring_storage import delete_api_key_from_keyring, is_keyring_available

    # Delete from keyring
    if is_keyring_available():
        delete_api_key_from_keyring()

    # Also remove from settings file if present (legacy cleanup)
    _remove_legacy_api_key_from_file()
