import os
import queue
import threading
import tkinter as tk
import webbrowser

import customtkinter as ctk
from screeninfo import get_monitors

from config import (
    AUTO_STOP_INACTIVITY_SECONDS,
    GUI_TRANSLATIONS_DIR,
    ICON_PATH,
    ICON_PATH_PNG,
)
from gui.announce_view import AnnounceViewMixin
from gui.audio_level_bar import AudioLevelBar
from gui.batch_view import BatchViewMixin
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
from gui.device_list import get_input_devices
from gui.dropdown import CustomDropdown
from gui.history_view import HistoryViewMixin
from gui.scaling import apply_display_scaling
from gui.settings_view import SettingsViewMixin
from gui.subtitle_window import SubtitleWindow
from gui.widgets import WidgetFactoryMixin
from providers import (
    PROVIDER_CHOICES,
    TRANSCRIPTION_PROVIDER_CHOICES,
    get_default_model,
    get_model_choices,
    get_stored_api_key,
    get_streaming_key_provider,
    has_usable_key,
)
from providers.openai.client import set_api_key
from utils.api_key_manager import (
    apply_dark_titlebar,
    prompt_for_api_key,
)
from utils.icons import ICO_SUPPORTED, scaled_icon_photo
from utils.json_helpers import load_json
from utils.logging import log, log_queue
from utils.settings import (
    DEFAULT_AI_PROVIDER,
    DEFAULT_GUI_LANGUAGE,
    DEFAULT_SEGMENTED_TRANSCRIPTION_PROVIDER,
    DEFAULT_STREAMING_TRANSCRIPTION_PROVIDER,
    GUI_LANGUAGE_CODES,
    PIPELINE_MODE_SEGMENTED,
    PIPELINE_MODE_STREAMING,
    SOURCE_LANGUAGES,
    STREAMING_TRANSCRIPTION_PROVIDERS,
    SUBTITLE_MODE_CONTINUOUS,
    SUBTITLE_MODE_REALTIME,
    SUBTITLE_MODE_STATIC,
    TARGET_LANGUAGE_DISPLAY_NAMES,
    TARGET_LANGUAGE_NAMES,
    language_canonical_name,
    language_display_name,
    load_settings,
    save_settings,
)
from utils.update_check import UpdateInfo, check_for_update
from version import __version__


def load_gui_translations(language: str) -> dict:
    en_path = os.path.join(GUI_TRANSLATIONS_DIR, "en.json")
    try:
        base = load_json(en_path)
    except Exception:
        base = {}

    if language == "en" or language not in GUI_LANGUAGE_CODES:
        return base

    path = os.path.join(GUI_TRANSLATIONS_DIR, f"{language}.json")
    try:
        return {**base, **load_json(path)}
    except Exception:
        return base


def _clear_stale_scaling_windows(force: bool = False) -> None:
    """Drop leftover windows from CustomTkinter's global ScalingTracker.

    First-run startup builds the onboarding wizard on its own CTk root and
    destroys it before this window is created. The wizard's frames linger in
    ScalingTracker's class-level registry, so ``ctk.set_widget_scaling()``
    (called before this window's own root exists) walks the dead canvases and
    raises ``TclError: invalid command name ...!ctkcanvas``, crashing startup.

    ``force=True`` clears every registered window unconditionally — safe only
    while the main window's root does not exist yet, when any registered window
    is by definition a leftover. Otherwise only windows that no longer exist are
    removed. Best-effort: an object that can't be introspected is treated as
    dead and dropped.
    """
    try:
        from customtkinter.windows.widgets.scaling.scaling_tracker import (
            ScalingTracker,
        )
    except Exception:
        return
    for name in ("window_widgets_dict", "window_dpi_scaling_dict"):
        registry = getattr(ScalingTracker, name, None)
        if not isinstance(registry, dict):
            continue
        for window in list(registry.keys()):
            if not force:
                try:
                    if window.winfo_exists():
                        continue
                except Exception:
                    pass
            registry.pop(window, None)


class AppGUI(
    AnnounceViewMixin,
    BatchViewMixin,
    HistoryViewMixin,
    SettingsViewMixin,
    WidgetFactoryMixin,
    ctk.CTk,
):
    # Minimum window size in CTk logical units, shared by _setup_window and
    # _toggle_log_panel (a mismatch there once locked the window at the
    # larger size after a log toggle).
    _MIN_W = 880
    _MIN_H = 536
    # Widest the collapsed 2-column card grid may grow (logical units, ≈1.4×
    # the design width) before extra window width becomes centered margin
    # instead of stretching the cards. See _collapsed_margin.
    _MAX_CARD_AREA_W = 1200

    def __init__(self, controller):
        self._saved_settings = load_settings()
        self._repair_default_provider()
        self._theme_mode = getattr(self._saved_settings, "theme_mode", "dark")
        ctk.set_appearance_mode(self._theme_mode)
        ctk.set_default_color_theme("green")
        self._responsive_scale = 0.86
        # A first-run session destroys the onboarding wizard (its own CTk root)
        # just before this; its dead frames linger in CTk's global
        # ScalingTracker and set_widget_scaling() would crash walking them.
        _clear_stale_scaling_windows()
        try:
            ctk.set_widget_scaling(self._responsive_scale)
        except tk.TclError:
            # Our own root isn't built yet, so any remaining registered window
            # is a leftover — clear them all and retry (an empty tracker can't
            # crash the callback walk).
            _clear_stale_scaling_windows(force=True)
            ctk.set_widget_scaling(self._responsive_scale)

        super().__init__()

        # The root exists now, so its monitor's DPI is known: clamp the global
        # scaling if this screen is too small for the design (see gui/scaling).
        # Must precede _setup_window() — geometry/minsize are scaled by it.
        self._responsive_scale = apply_display_scaling(self, self._responsive_scale)

        self.controller = controller
        self.gui_lang_code = self._saved_settings.gui_language or DEFAULT_GUI_LANGUAGE
        self._gui_lang = self.gui_lang_code
        self.gui_texts = load_gui_translations(self.gui_lang_code)
        self._t = self.gui_texts
        self._colors = self._palette(self._theme_mode)

        self.translation_queue = self.controller.translation_queue
        self.error_queue = self.controller.error_queue

        self.selected_screen_index = self._saved_settings.monitor_index
        self.subtitle_window: SubtitleWindow | None = None
        self.translation_poll_job: str | None = None
        self.error_poll_job: str | None = None
        self.log_poll_job: str | None = None
        self.height_apply_job: str | None = None
        self.inactivity_check_job: str | None = None
        # Compact input-level meter (backend: controller.get_input_level()).
        self.input_level_poll_job: str | None = None
        self._input_level_ui_state: tuple[bool, bool] | None = None

        # Startup update check (worker thread writes, after-poll reads).
        self._update_check_result: UpdateInfo | None = None
        self._update_check_done = False
        self._update_poll_job: str | None = None
        self._update_poll_tries = 0
        self._update_available: UpdateInfo | None = None

        self._running = False
        self._log_polling = False
        self._surface_restore_job: str | None = None
        self.speed_value = max(0.5, min(5.0, self._saved_settings.scroll_speed))
        self.advanced_visible = False
        self._log_collapsed = self._saved_settings.log_panel_collapsed

        self._shadow_frames: list[ctk.CTkFrame] = []
        self._cards: list[ctk.CTkFrame] = []
        self._main_panels: list[ctk.CTkFrame] = []
        self._labels: list[ctk.CTkLabel] = []
        self._muted_labels: list[ctk.CTkLabel] = []
        self._section_titles: list[ctk.CTkLabel] = []
        self._symbol_labels: list[ctk.CTkLabel] = []
        self._buttons: list[ctk.CTkButton] = []
        self._combos: list[CustomDropdown] = []
        self._checkboxes: list[ctk.CTkCheckBox] = []

        self._setup_window()
        self._create_layout()
        if not self._saved_settings.hide_subtitle_on_stop:
            self._create_subtitle_window()
        self._finalize_setup()

    def _clean_action_label(self, key: str) -> str:
        text = self.gui_texts.get(key, key)
        return text.lstrip("▶■● ").strip()

    def _mode_label(self, mode: str) -> str:
        labels = {
            SUBTITLE_MODE_REALTIME: self.gui_texts.get(
                "subtitle_mode_realtime", "Realtime"
            ),
            SUBTITLE_MODE_CONTINUOUS: self.gui_texts.get(
                "subtitle_mode_continuous", "Continuous"
            ),
            SUBTITLE_MODE_STATIC: self.gui_texts.get("subtitle_mode_static", "Static"),
        }
        return labels.get(mode, mode)

    def _setup_window(self) -> None:
        self.title(f"MinbarLive v{__version__}")
        # Collapsed (log hidden) is a wide 2-column card grid; expanded (log
        # shown) is the classic single-column sidebar + log panel.
        # CTk geometry is in DPI-logical units (physical = logical x window
        # scaling, e.g. x1.25). 880x576 logical renders ~1100x720 px, which is
        # the compact first-run size the 2-column card layout fits snugly.
        default_geo = f"{self._MIN_W}x{self._MIN_H}"
        _min_w, _min_h = self._MIN_W, self._MIN_H

        saved_geo = self._saved_settings.window_geometry or ""
        if saved_geo:
            # Validate the saved geometry is still on an accessible monitor
            try:
                import re

                from screeninfo import get_monitors

                m = re.fullmatch(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", saved_geo)
                if m:
                    gw, gh, gx, gy = (
                        int(m.group(1)),
                        int(m.group(2)),
                        int(m.group(3)),
                        int(m.group(4)),
                    )
                    monitors = get_monitors()
                    on_screen = any(
                        mon.x <= gx < mon.x + mon.width
                        and mon.y <= gy < mon.y + mon.height
                        for mon in monitors
                    )
                    if on_screen and gw >= _min_w and gh >= _min_h:
                        self.geometry(saved_geo)
                    else:
                        self.geometry(default_geo)
                else:
                    self.geometry(default_geo)
            except Exception:
                self.geometry(default_geo)
        else:
            self.geometry(default_geo)

        self.minsize(_min_w, _min_h)
        self.configure(fg_color=self._colors["app_bg"])
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after_idle(self._restore_control_window_surface)
        self.bind("<FocusIn>", self._schedule_control_window_surface_restore)
        self.bind("<Map>", self._schedule_control_window_surface_restore)

        if ICO_SUPPORTED and os.path.exists(ICON_PATH):
            try:
                self.iconbitmap(ICON_PATH)
            except Exception:
                pass
        elif os.path.exists(ICON_PATH_PNG):
            try:
                self.iconphoto(False, scaled_icon_photo(ICON_PATH_PNG))
            except Exception:
                pass

        if self._log_collapsed:
            self.grid_columnconfigure(0, weight=1, minsize=self._MIN_W)
            self.grid_columnconfigure(1, weight=0)
        else:
            self.grid_columnconfigure(0, weight=0, minsize=500)
            self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

    def _schedule_control_window_surface_restore(
        self, _event: object | None = None
    ) -> None:
        if self._surface_restore_job is not None:
            return
        self._surface_restore_job = self.after(
            120, self._restore_control_window_surface
        )

    def _restore_control_window_surface(self, _event: object | None = None) -> None:
        """Keep the control panel opaque and above the subtitle overlay."""
        self._surface_restore_job = None
        try:
            self.wm_attributes("-transparentcolor", "")
        except tk.TclError:
            pass
        try:
            self.attributes("-alpha", 1.0)
        except tk.TclError:
            pass
        try:
            self.attributes("-topmost", self._control_window_should_be_topmost())
            self.lift()
        except tk.TclError:
            pass

    def _control_window_should_be_topmost(self) -> bool:
        """The control panel only needs to float above the subtitle overlay
        while that overlay is open — and only if the user hasn't turned
        always-on-top off. With no overlay open it stays in normal stacking."""
        if not self._saved_settings.always_on_top:
            return False
        return bool(self.subtitle_window and self.subtitle_window.winfo_exists())

    def _apply_control_window_topmost(self) -> None:
        """Re-apply the control panel's topmost attribute after the subtitle
        overlay opens/closes or the always-on-top setting changes."""
        try:
            self.attributes("-topmost", self._control_window_should_be_topmost())
        except tk.TclError:
            pass

    def _create_layout(self) -> None:
        # Fixed container for the left column (header + scrollable cards)
        self.sidebar_container = ctk.CTkFrame(
            self, fg_color=self._colors["sidebar"], corner_radius=0
        )
        self.sidebar_container.grid(row=0, column=0, sticky="nsew")
        self.sidebar_container.grid_columnconfigure(0, weight=1)
        self.sidebar_container.grid_rowconfigure(2, weight=1)

        self._create_sidebar_header()
        self._create_update_banner()

        self.sidebar = ctk.CTkScrollableFrame(
            self.sidebar_container,
            fg_color=self._colors["sidebar"],
            corner_radius=0,
        )
        self.sidebar.grid(row=2, column=0, sticky="nsew")
        self.sidebar.grid_columnconfigure(0, weight=1)
        # Cap + center the collapsed card grid whenever the window resizes.
        self._applied_collapsed_margin: int | None = None
        self.sidebar.bind("<Configure>", self._on_sidebar_resize, add="+")

        self.content = ctk.CTkFrame(
            self, fg_color=self._colors["app_bg"], corner_radius=0
        )
        self.content.grid(row=0, column=1, sticky="nsew", padx=(18, 22), pady=20)
        self.content.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(1, weight=1)

        # Two independent card columns. A single shared-row grid couples the
        # two columns' row heights, so a short card (Controls) next to a tall
        # one (Translation flow) gets padded out with dead space — the empty
        # bottom-right the old 2×2 grid showed. Give each column its own frame
        # so it packs top-down on its own. Left = Controls + Display; right =
        # Translation flow + Advanced, chosen so the columns end level.
        self._col_left = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        self._col_right = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        self._col_left.grid_columnconfigure(0, weight=1)
        self._col_right.grid_columnconfigure(0, weight=1)

        self._init_batch_state()
        self._init_announce_state()
        self._create_control_card()
        self._create_language_card()
        self._create_display_card()
        self._create_advanced_card()
        self._create_log_panel()
        # Batch/File is reached from the header icon (a Toplevel), not a card.
        self._batch_win: ctk.CTkToplevel | None = None

        # Each column reserves a weighted spacer row (row 1) between its top and
        # bottom card. When the columns are placed side by side (sticky "nsew"),
        # grid stretches the shorter column up to the taller one's height and the
        # extra height lands in this spacer row, pushing that column's bottom card
        # down so both bottom cards always end level — no matter which column is
        # taller (Display when stopped, Advanced + running hint when running).
        # See _layout_sidebar_cards.
        self._col_left.grid_rowconfigure(1, weight=1)
        self._col_right.grid_rowconfigure(1, weight=1)

        self.control_card.grid(row=0, column=0, sticky="new")
        self.display_card.grid(row=2, column=0, sticky="sew", pady=(18, 0))
        self.language_card.grid(row=0, column=0, sticky="new")
        self.advanced_card.grid(row=2, column=0, sticky="sew", pady=(18, 0))
        self._layout_sidebar_cards()

        if self._log_collapsed:
            self.content.grid_remove()

    def _layout_sidebar_cards(self) -> None:
        """Position the two card columns: side by side (2-column masonry) while
        the log panel is collapsed, stacked into one column (classic look) when
        it is open. In the side-by-side layout both columns are stretched to the
        same (taller) height via sticky "nsew"; each column's weighted spacer row
        then drops its bottom card so the two columns always end level. Stacked,
        the columns keep their natural heights (sticky "new")."""
        two_col = self._log_collapsed
        if two_col:
            self.sidebar.grid_columnconfigure(0, weight=1, uniform="sbcols")
            self.sidebar.grid_columnconfigure(1, weight=1, uniform="sbcols")
            # Equal-height / bottom-aligned columns ("nsew") only while the
            # Advanced card is collapsed — then the two columns are close in
            # height and level bottoms look clean. When Advanced is expanded the
            # right column is far taller; stretching the left column to match
            # would strand "Anzeige & Audio" floating in the middle, so
            # top-anchor both columns instead ("new" — natural masonry stacking).
            col_sticky = "new" if getattr(self, "advanced_visible", False) else "nsew"
            # columnspan is stated explicitly: grid() retains prior options, and
            # the single-column branch below leaves columnspan=2 behind — without
            # resetting it the two columns would overlap after a log toggle.
            margin = self._collapsed_margin()
            self._applied_collapsed_margin = margin
            self._col_left.grid(
                row=0,
                column=0,
                columnspan=1,
                sticky=col_sticky,
                padx=(18 + margin, 9),
                pady=18,
            )
            self._col_right.grid(
                row=0,
                column=1,
                columnspan=1,
                sticky=col_sticky,
                padx=(9, 18 + margin),
                pady=18,
            )
        else:
            self._applied_collapsed_margin = None
            self.sidebar.grid_columnconfigure(0, weight=1, uniform="")
            self.sidebar.grid_columnconfigure(1, weight=0, uniform="")
            self._col_left.grid(
                row=0, column=0, columnspan=2, sticky="new", padx=18, pady=(18, 0)
            )
            self._col_right.grid(
                row=1, column=0, columnspan=2, sticky="new", padx=18, pady=(18, 18)
            )

    def _collapsed_margin(self) -> int:
        """Extra outer padding (raw px) that caps the 2-column card grid at a
        comfortable width and centers it, so maximizing the collapsed window
        doesn't stretch the cards across the whole screen. 0 until the sidebar
        is mapped; a <Configure> binding re-applies the layout on resize."""
        try:
            avail = self.sidebar.winfo_width()
            scaling = self._get_window_scaling()
        except Exception:
            return 0
        if avail <= 1:  # not yet mapped
            return 0
        max_px = int(self._MAX_CARD_AREA_W * scaling)
        return max(0, (avail - max_px) // 2)

    def _on_sidebar_resize(self, _event: object | None = None) -> None:
        """Re-center the collapsed card grid when the window is resized."""
        if not self._log_collapsed:
            return
        margin = self._collapsed_margin()
        if margin == getattr(self, "_applied_collapsed_margin", None):
            return
        self._applied_collapsed_margin = margin
        self._col_left.grid_configure(padx=(18 + margin, 9))
        self._col_right.grid_configure(padx=(9, 18 + margin))

    def _create_sidebar_header(self) -> None:
        header = ctk.CTkFrame(
            self.sidebar_container,
            fg_color=self._colors["sidebar"],
            height=68,
            corner_radius=0,
        )
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        header.grid_propagate(False)
        self._sidebar_header = header

        title_label = ctk.CTkLabel(
            header,
            text="MinbarLive",
            font=ctk.CTkFont(family="Segoe UI", size=20, weight="bold"),
            text_color=self._colors["text"],
        )
        title_label.grid(row=0, column=0, sticky="w", padx=22, pady=16)
        self._labels.append(title_label)

        self._history_btn = ctk.CTkButton(
            header,
            text="⟲",
            command=self._open_history_window,
            width=44,
            height=44,
            corner_radius=14,
            font=ctk.CTkFont(family="Segoe UI Symbol", size=20),
            fg_color=self._colors["button"],
            hover_color=self._colors["button_hover"],
            text_color=self._colors["text"],
        )
        self._history_btn.grid(row=0, column=1, sticky="e", padx=(0, 8), pady=12)
        self._buttons.append(self._history_btn)

        self._batch_btn = ctk.CTkButton(
            header,
            text="▦",
            command=self._open_batch_window,
            width=44,
            height=44,
            corner_radius=14,
            font=ctk.CTkFont(family="Segoe UI Symbol", size=20),
            fg_color=self._colors["button"],
            hover_color=self._colors["button_hover"],
            text_color=self._colors["text"],
        )
        self._batch_btn.grid(row=0, column=2, sticky="e", padx=(0, 8), pady=12)
        self._buttons.append(self._batch_btn)

        self._announce_btn = ctk.CTkButton(
            header,
            text="📣",
            command=self._open_announce_window,
            width=44,
            height=44,
            corner_radius=14,
            font=ctk.CTkFont(family="Segoe UI Symbol", size=20),
            fg_color=self._colors["button"],
            hover_color=self._colors["button_hover"],
            text_color=self._colors["text"],
        )
        self._announce_btn.grid(row=0, column=3, sticky="e", padx=(0, 8), pady=12)
        self._buttons.append(self._announce_btn)

        self._settings_btn = ctk.CTkButton(
            header,
            text="⚙",
            command=self._open_settings_window,
            width=44,
            height=44,
            corner_radius=14,
            font=ctk.CTkFont(family="Segoe UI Symbol", size=20),
            fg_color=self._colors["button"],
            hover_color=self._colors["button_hover"],
            text_color=self._colors["text"],
        )
        self._settings_btn.grid(row=0, column=4, sticky="e", padx=(0, 8), pady=12)
        self._buttons.append(self._settings_btn)

        self._log_toggle_btn = ctk.CTkButton(
            header,
            text="◀" if not self._log_collapsed else "▶",
            command=self._toggle_log_panel,
            width=44,
            height=44,
            corner_radius=14,
            font=ctk.CTkFont(family="Segoe UI Symbol", size=16),
            fg_color=self._colors["button"],
            hover_color=self._colors["button_hover"],
            text_color=self._colors["text"],
        )
        self._log_toggle_btn.grid(row=0, column=5, sticky="e", padx=(0, 16), pady=12)
        self._buttons.append(self._log_toggle_btn)

    # ── Update notice ───────────────────────────────────────────────────────
    # Dismissible banner between the header and the cards, shown when the
    # startup check (one anonymous GET to the GitHub releases API, opt-out
    # via the check_for_updates setting) finds a newer release. Clicking it
    # opens the release page; it never blocks or interrupts anything.

    def _create_update_banner(self) -> None:
        banner = ctk.CTkFrame(
            self.sidebar_container,
            fg_color=self._colors["accent_soft"],
            corner_radius=14,
        )
        banner.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 4))
        banner.grid_columnconfigure(0, weight=1)

        label = ctk.CTkLabel(
            banner,
            text="",
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            text_color=self._colors["accent"],
            anchor="w",
            justify="left",
            cursor="hand2",
        )
        label.grid(row=0, column=0, sticky="ew", padx=(14, 8), pady=8)

        close_btn = ctk.CTkButton(
            banner,
            text="✕",
            command=self._dismiss_update_banner,
            width=28,
            height=28,
            corner_radius=14,
            font=ctk.CTkFont(family="Segoe UI Symbol", size=13),
            fg_color="transparent",
            hover=False,
            text_color=self._colors["accent"],
        )
        close_btn.grid(row=0, column=1, sticky="e", padx=(0, 8), pady=6)

        banner.bind("<Button-1>", self._open_release_page)
        label.bind("<Button-1>", self._open_release_page)
        banner.configure(cursor="hand2")
        banner.grid_remove()  # hidden until a newer release is confirmed

        self._update_banner = banner
        self._update_banner_label = label
        self._update_banner_close = close_btn

    def _start_update_check(self) -> None:
        if not self._saved_settings.check_for_updates:
            return

        def worker() -> None:
            # check_for_update never raises; result lands via the after-poll
            # below (Tk widgets must not be touched from a worker thread).
            self._update_check_result = check_for_update()
            self._update_check_done = True

        threading.Thread(target=worker, daemon=True, name="UPDATE-CHECK").start()
        self._update_poll_job = self.after(2000, self._poll_update_check)

    def _poll_update_check(self) -> None:
        self._update_poll_job = None
        if not self._update_check_done:
            self._update_poll_tries += 1
            if self._update_poll_tries < 30:  # give up after ~1 min
                self._update_poll_job = self.after(2000, self._poll_update_check)
            return
        info = self._update_check_result
        if info is not None:
            log(f"Update available: v{info.version}", level="INFO")
            self._show_update_banner(info)

    def _show_update_banner(self, info: UpdateInfo) -> None:
        self._update_available = info
        self._update_banner_label.configure(text=self._update_banner_text())
        self._update_banner.grid()

    def _update_banner_text(self) -> str:
        template = self.gui_texts.get(
            "update_available", "Version {version} available — click to download"
        )
        try:
            return template.format(version=self._update_available.version)
        except Exception:
            return template

    def _dismiss_update_banner(self) -> None:
        self._update_banner.grid_remove()

    def _open_release_page(self, _event: object | None = None) -> None:
        if self._update_available is not None:
            webbrowser.open(self._update_available.url)

    def _create_control_card(self) -> None:
        card = self._section_card(self._col_left, "▶", "control_center")
        self.control_card = card
        card.grid_columnconfigure(0, weight=1, uniform="control_actions")
        card.grid_columnconfigure(1, weight=1, uniform="control_actions")

        self.status_badge = ctk.CTkFrame(
            card, fg_color=self._colors["danger_soft"], corner_radius=999
        )
        self.status_badge.grid(
            row=2, column=0, columnspan=2, sticky="ew", padx=16, pady=(0, 10)
        )
        self.status_label = ctk.CTkLabel(
            self.status_badge,
            text=self.gui_texts.get("stopped", "Stopped"),
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color=self._colors["danger"],
        )
        self.status_label.pack(fill="x", padx=12, pady=6)

        self.start_btn = ctk.CTkButton(
            card,
            text=f"▶  {self._clean_action_label('start')}",
            command=self.on_start,
            width=0,
            height=56,
            corner_radius=18,
            font=ctk.CTkFont(family="Segoe UI", size=18, weight="bold"),
            fg_color=self._colors["accent"],
            hover_color=self._colors["accent_hover"],
            text_color="#ffffff",
            text_color_disabled=self._colors["muted"],
        )
        self.start_btn.grid(row=3, column=0, sticky="ew", padx=(16, 6), pady=(0, 14))

        self.stop_btn = ctk.CTkButton(
            card,
            text=f"■  {self._clean_action_label('stop')}",
            command=self.on_stop,
            width=0,
            height=56,
            corner_radius=18,
            font=ctk.CTkFont(family="Segoe UI", size=18, weight="bold"),
            fg_color=self._colors["button"],
            hover_color=self._colors["button_hover"],
            text_color="#ffffff",
            text_color_disabled=self._colors["muted"],
            state="disabled",
        )
        self.stop_btn.grid(row=3, column=1, sticky="ew", padx=(6, 16), pady=(0, 14))

    def _create_display_card(self) -> None:
        card = self._section_card(self._col_left, "▤", "display_routing")
        self.display_card = card
        card.grid_columnconfigure(0, weight=1, uniform="display_cols")
        card.grid_columnconfigure(1, weight=1, uniform="display_cols")

        # ── Subtitle Screen (left) and Input Device (right) side-by-side ─────
        screen_frame = self._field(
            card, "subtitle_screen", "▣", row=2, column=0, columnspan=1, padx=(18, 8)
        )
        screen_names = self._get_screen_names()
        self.screen_combo = self._combo(
            screen_frame,
            values=screen_names,
            command=lambda _value: self._on_screen_change(),
        )
        if screen_names:
            self.screen_combo.current(
                min(self.selected_screen_index, len(screen_names) - 1)
            )
        self.screen_combo.pack(fill="x", pady=(8, 0))

        device_frame = self._field(
            card, "input_device", "◉", row=2, column=1, columnspan=1, padx=(8, 18)
        )
        self.device_names, self.device_base_names, self.device_indices, self.device_loopback_flags = (
            self._get_input_devices()
        )
        self.device_combo = self._combo(
            device_frame,
            values=self.device_names,
            command=lambda _value: self._on_device_change(),
        )
        if self.device_names:
            selected_device = 0
            saved_name = self._saved_settings.input_device_name
            if saved_name in self.device_base_names:
                selected_device = self.device_base_names.index(saved_name)
            elif saved_name in self.device_names:
                selected_device = self.device_names.index(saved_name)
            self.device_combo.current(selected_device)
        self.device_combo.pack(fill="x", pady=(8, 0))

        # Full-width input-level row below both dropdowns (keeps the monitor and
        # input dropdowns the same height; the meter is one level, not stacked).
        self._build_input_level_meter(card)

        font_frame = self._mini_panel(card)
        font_frame.grid(row=4, column=0, sticky="nsew", padx=(16, 6), pady=(0, 12))
        self.font_label = self._label(font_frame, "font", size=14, weight="bold")
        self.font_label.pack(anchor="w", padx=12, pady=(10, 4))
        font_buttons = ctk.CTkFrame(font_frame, fg_color="transparent")
        font_buttons.pack(fill="x", padx=12, pady=(0, 10))
        font_buttons.grid_columnconfigure(0, weight=1)
        font_buttons.grid_columnconfigure(1, weight=1)
        self.font_decrease_btn = self._plain_button(
            font_buttons, "−", self._decrease_subtitle_font, height=44
        )
        self.font_decrease_btn.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        self.font_increase_btn = self._plain_button(
            font_buttons, "+", self._increase_subtitle_font, height=44
        )
        self.font_increase_btn.grid(row=0, column=1, sticky="ew", padx=(5, 0))

        height_frame = self._mini_panel(card)
        height_frame.grid(row=4, column=1, sticky="nsew", padx=(6, 16), pady=(0, 12))
        self.height_label = self._label(height_frame, "height", size=14, weight="bold")
        self.height_label.pack(anchor="w", padx=12, pady=(10, 4))
        self.height_value_label = ctk.CTkLabel(
            height_frame,
            text=f"{self._saved_settings.window_height_percent}%",
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color=self._colors["text"],
        )
        self.height_value_label.pack(anchor="w", padx=12)
        self.height_slider = ctk.CTkSlider(
            height_frame,
            from_=0,
            to=100,
            number_of_steps=100,
            command=self._on_height_slider_change,
            button_color=self._colors["accent"],
            progress_color=self._colors["accent"],
            fg_color=self._colors["button"],
            button_hover_color=self._colors["accent_hover"],
        )
        self.height_slider.set(self._saved_settings.window_height_percent)
        self.height_slider.pack(fill="x", padx=12, pady=(6, 12))

        # Run without an audience overlay (transcription/translation still run).
        self.subtitle_output_var = tk.BooleanVar(
            value=self._saved_settings.subtitle_output_enabled
        )
        self.subtitle_output_cb = self._checkbox(
            card,
            "subtitle_output_enabled",
            self.subtitle_output_var,
            self._on_subtitle_output_change,
        )
        self.subtitle_output_cb.grid(
            row=5, column=0, columnspan=2, sticky="w", padx=18, pady=(0, 14)
        )

    def _on_subtitle_output_change(self) -> None:
        enabled = self.subtitle_output_var.get()
        self._saved_settings.subtitle_output_enabled = enabled
        self._save_current_settings()
        if not enabled:
            self._destroy_subtitle_window()
        elif self._running or not self._saved_settings.hide_subtitle_on_stop:
            if not (self.subtitle_window and self.subtitle_window.winfo_exists()):
                self._create_subtitle_window()
        log(
            f"Subtitle output on monitor: {'on' if enabled else 'off'}",
            level="INFO",
        )

    # Conventional audio-meter zone colours (green/amber/red in both themes).
    _INPUT_LEVEL_GREEN = "#37B24D"
    _INPUT_LEVEL_WARNING = "#F08C00"
    _INPUT_LEVEL_DANGER = "#E03131"

    def _build_input_level_meter(self, card: ctk.CTkFrame) -> None:
        """Full-width live input-level row below the monitor/input dropdowns.

        One level line: dBFS readout · segmented bar (stretches) · Test button.
        Reads controller.get_input_level() (fed by the live capture or a
        meter-only preview) — see the reliability/meter backend unit.
        """
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.grid(row=3, column=0, columnspan=2, sticky="ew", padx=18, pady=(0, 14))
        row.grid_columnconfigure(1, weight=1)  # bar stretches; label/button fixed

        self.input_level_value_label = ctk.CTkLabel(
            row,
            text=self.gui_texts.get("input_level_no_signal", "No signal"),
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=self._colors["muted"],
            width=84,
            anchor="w",
        )
        self.input_level_value_label.grid(row=0, column=0, sticky="w", padx=(0, 10))
        self._muted_labels.append(self.input_level_value_label)

        self.input_level_bar = AudioLevelBar(
            row,
            track_color=self._colors["panel_soft"],
            border_color=self._colors["border"],
            green_color=self._INPUT_LEVEL_GREEN,
            warning_color=self._INPUT_LEVEL_WARNING,
            danger_color=self._INPUT_LEVEL_DANGER,
            height=12,
        )
        self.input_level_bar.grid(row=0, column=1, sticky="ew", padx=(0, 12))

        self.input_level_test_btn = ctk.CTkButton(
            row,
            text=self.gui_texts.get("input_level_test", "Test mic"),
            command=self._toggle_input_level_test,
            width=110,
            height=28,
            corner_radius=14,
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            fg_color=self._colors["button"],
            hover_color=self._colors["button_hover"],
            text_color=self._colors["text"],
        )
        self.input_level_test_btn.grid(row=0, column=2, sticky="e")

        self._input_level_ui_state = None
        self.input_level_poll_job = self.after(200, self._poll_input_level)

    def _poll_input_level(self) -> None:
        try:
            snapshot = self.controller.get_input_level()
        except Exception:
            snapshot = None
        try:
            if snapshot is not None and hasattr(self, "input_level_bar"):
                rms_dbfs = snapshot.rms_dbfs
                value = max(0.0, min(1.0, (rms_dbfs + 60.0) / 60.0))
                self.input_level_bar.set(value)
                if snapshot.clipping_ratio > 0.02:
                    text = self.gui_texts.get("input_level_clipping", "Clipping!")
                    color = self._colors["danger"]
                elif value <= 0.001:
                    text = self.gui_texts.get("input_level_no_signal", "No signal")
                    color = self._colors["muted"]
                else:
                    text = f"{rms_dbfs:.0f} dBFS"
                    color = self._colors["text"]
                self.input_level_value_label.configure(text=text, text_color=color)
                self._sync_input_level_button()
            self.input_level_poll_job = self.after(120, self._poll_input_level)
        except tk.TclError:
            # Window/widgets torn down between ticks — stop quietly (the
            # on_close after-sweep and this early return both end the loop).
            pass

    def _sync_input_level_button(self) -> None:
        if not hasattr(self, "input_level_test_btn"):
            return
        testing = False
        checker = getattr(self.controller, "is_input_level_test_running", None)
        if checker is not None:
            try:
                testing = bool(checker())
            except Exception:
                testing = False
        state = (self._running, testing)
        if state == self._input_level_ui_state:
            return
        self._input_level_ui_state = state
        if self._running:
            # A live session already feeds the meter; a preview cannot own the
            # same device, so testing is not offered while running.
            self.input_level_test_btn.configure(
                text=self.gui_texts.get("input_level_test", "Test mic"),
                state="disabled",
            )
            return
        self.input_level_test_btn.configure(
            state="normal",
            text=(
                self.gui_texts.get("input_level_stop_test", "Stop")
                if testing
                else self.gui_texts.get("input_level_test", "Test mic")
            ),
        )

    def _toggle_input_level_test(self) -> None:
        controller = self.controller
        checker = getattr(controller, "is_input_level_test_running", None)
        if checker is not None and checker():
            try:
                controller.stop_input_level_test()
            except Exception:
                pass
            self._input_level_ui_state = None
            return
        if self._running:
            return
        try:
            controller.start_input_level_test(self.get_selected_device_index())
        except Exception as exc:
            self._alert(
                self.gui_texts.get("input_level", "Input level"),
                str(exc),
                danger=True,
            )
        self._input_level_ui_state = None

    def _create_language_card(self) -> None:
        card = self._section_card(self._col_right, "⇄", "translation_flow")
        self.language_card = card
        card.grid_columnconfigure(0, weight=1)

        # ── Source + Swap + Target — all on one row ─────────────────────────
        lang_pair_frame = ctk.CTkFrame(card, fg_color="transparent")
        lang_pair_frame.grid(row=2, column=0, sticky="ew", padx=18, pady=(0, 18))
        lang_pair_frame.grid_columnconfigure(0, weight=1, uniform="lang_pair")
        lang_pair_frame.grid_columnconfigure(1, weight=0)
        lang_pair_frame.grid_columnconfigure(2, weight=1, uniform="lang_pair")

        # Source sub-frame
        source_sub = ctk.CTkFrame(lang_pair_frame, fg_color="transparent")
        source_sub.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        source_sub.grid_columnconfigure(0, weight=1)
        source_label = self._label(
            source_sub, "source", symbol="⌁", size=14, weight="bold"
        )
        source_label.grid(row=0, column=0, sticky="w")
        # Canonical (English) names drive storage/lookups; the dropdown shows
        # the native endonym via language_display_name().
        self._source_lang_names = [name for name, _code in SOURCE_LANGUAGES]
        self.source_lang_combo = self._combo(
            source_sub,
            values=[language_display_name(n) for n in self._source_lang_names],
            command=lambda _value: self._on_source_language_change(),
        )
        self.source_lang_combo.set(
            language_display_name(self._saved_settings.source_language)
        )
        self.source_lang_combo.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        # Real-time mode has no auto-detect → hide "Automatic" while streaming.
        self._refresh_source_language_combo()

        # Swap button (vertically aligned with combos)
        self.swap_btn = self._plain_button(
            lang_pair_frame, "⇄", self._on_swap_languages, height=46, width=50
        )
        self.swap_btn.grid(row=0, column=1, padx=6, pady=(22, 0))

        # Target sub-frame
        target_sub = ctk.CTkFrame(lang_pair_frame, fg_color="transparent")
        target_sub.grid(row=0, column=2, sticky="ew", padx=(4, 0))
        target_sub.grid_columnconfigure(0, weight=1)
        target_label = self._label(
            target_sub, "target", symbol="→", size=14, weight="bold"
        )
        target_label.grid(row=0, column=0, sticky="w")
        self.language_combo = self._combo(
            target_sub,
            values=TARGET_LANGUAGE_DISPLAY_NAMES,
            command=lambda _value: self._on_language_change(),
        )
        self.language_combo.set(
            language_display_name(self._saved_settings.target_language)
        )
        self.language_combo.grid(row=1, column=0, sticky="ew", pady=(4, 0))

        # ── Subtitle Mode + Mode Controls — side by side ─────────────────────
        mode_outer = ctk.CTkFrame(card, fg_color="transparent")
        mode_outer.grid(row=3, column=0, sticky="ew", padx=18, pady=(0, 12))
        mode_outer.grid_columnconfigure(0, weight=1)
        mode_outer.grid_columnconfigure(1, weight=0, minsize=160)

        # Mode combo (left)
        mode_sub = ctk.CTkFrame(mode_outer, fg_color="transparent")
        mode_sub.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        mode_sub.grid_columnconfigure(0, weight=1)
        mode_combo_label = self._label(
            mode_sub, "subtitles", symbol="≋", size=14, weight="bold"
        )
        mode_combo_label.grid(row=0, column=0, sticky="w")
        # Realtime is streaming-only, so the value list depends on the
        # Processing Strategy — _refresh_subtitle_mode_combo keeps it synced.
        self._subtitle_mode_values = self._subtitle_mode_choices()
        mode_values = [self._mode_label(mode) for mode in self._subtitle_mode_values]
        self.subtitle_mode_combo = self._combo(
            mode_sub,
            values=mode_values,
            command=lambda _value: self._on_subtitle_mode_change(),
        )
        saved_mode = self._effective_subtitle_mode()
        if saved_mode in self._subtitle_mode_values:
            self.subtitle_mode_combo.current(
                self._subtitle_mode_values.index(saved_mode)
            )
        else:
            self.subtitle_mode_combo.current(0)
        self.subtitle_mode_combo.grid(row=1, column=0, sticky="ew", pady=(4, 0))

        # Mode controls (right — vertically anchored to bottom of mode_sub, aligning with combo)
        self.mode_controls = ctk.CTkFrame(mode_outer, fg_color="transparent")
        self.mode_controls.grid(row=0, column=1, sticky="s")

        # Speed compact row (shown for Continuous mode)
        self.speed_row = ctk.CTkFrame(self.mode_controls, fg_color="transparent")
        self.speed_row.grid_columnconfigure(1, weight=0)
        self.speed_decrease_btn = self._plain_button(
            self.speed_row, "−", self._decrease_scroll_speed, height=46, width=46
        )
        self.speed_decrease_btn.grid(row=0, column=0)
        self.speed_label = ctk.CTkLabel(
            self.speed_row,
            text=f"{self.speed_value:.1f}x",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color=self._colors["text"],
            width=52,
        )
        self.speed_label.grid(row=0, column=1, padx=2)
        self.speed_increase_btn = self._plain_button(
            self.speed_row, "+", self._increase_scroll_speed, height=46, width=46
        )
        self.speed_increase_btn.grid(row=0, column=2)

        # Transparent checkbox (shown for Static mode)
        self.transparent_var = tk.BooleanVar(
            value=self._saved_settings.transparent_static
        )
        self.transparent_checkbox = self._checkbox(
            self.mode_controls,
            "transparent",
            self.transparent_var,
            self._on_transparent_change,
        )

        # ── Processing Strategy (master switch: real-time / chunk / semantic) ─
        # Sits below the Subtitles selector.
        strat_label_frame = ctk.CTkFrame(card, fg_color="transparent")
        strat_label_frame.grid(row=5, column=0, sticky="ew", padx=18, pady=(4, 2))
        strat_label_frame.grid_columnconfigure(0, weight=1)
        strat_lbl = self._label(
            strat_label_frame, "processing_strategy", symbol="⇶", size=14, weight="bold"
        )
        strat_lbl.pack(anchor="w")

        strat_combo_row = ctk.CTkFrame(card, fg_color="transparent")
        strat_combo_row.grid(row=6, column=0, sticky="ew", padx=18, pady=(0, 14))
        strat_combo_row.grid_columnconfigure(0, weight=1)
        # Master switch. "realtime" => streaming (pipeline_mode streaming);
        # "semantic"/"chunk" => segmented buffering. Real-time is first and the
        # fresh-install default; semantic precedes chunk (better rote Linie).
        self._strategy_ids = list(STRATEGY_IDS)
        self._strategy_display_names = self._strategy_labels()
        self.strategy_combo = self._combo(
            strat_combo_row,
            values=self._strategy_display_names,
            command=lambda _value: self._on_strategy_change(),
        )
        self.strategy_combo.current(self._current_strategy_index())
        self.strategy_combo.grid(row=0, column=0, columnspan=2, sticky="ew")

        self.strategy_running_hint = ctk.CTkLabel(
            strat_combo_row,
            text=self.gui_texts.get("hint_stop_to_change", "⚠ Stop program to change"),
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=self._colors["warning"],
            height=20,
        )
        self.strategy_running_hint.grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(2, 0)
        )
        self.strategy_running_hint.grid_remove()

        # Display toggles side by side: "Show original text" (bilingual, all
        # modes, default on) on the left; adaptive catch-up (continuous-only,
        # _update_speed_button_states() grid()/grid_remove()s it) on the
        # right. The old "Show live transcript" checkbox is gone — the live
        # line is now intrinsic to the Realtime subtitle mode.
        cb_row = ctk.CTkFrame(card, fg_color="transparent")
        cb_row.grid(row=7, column=0, sticky="ew", padx=18, pady=(0, 14))
        cb_row.grid_columnconfigure(0, weight=1, uniform="cbcols")
        cb_row.grid_columnconfigure(1, weight=1, uniform="cbcols")

        self.bilingual_var = tk.BooleanVar(value=self._saved_settings.bilingual_mode)
        self.bilingual_cb = self._checkbox(
            cb_row,
            "bilingual_mode",
            self.bilingual_var,
            self._on_bilingual_change,
        )
        self.bilingual_cb.grid(row=0, column=0, sticky="w")

        self.adaptive_catchup_var = tk.BooleanVar(
            value=self._saved_settings.adaptive_subtitle_catchup
        )
        self.adaptive_catchup_cb = self._checkbox(
            cb_row,
            "adaptive_subtitle_catchup",
            self.adaptive_catchup_var,
            self._on_adaptive_catchup_change,
        )
        self.adaptive_catchup_cb.grid(row=0, column=1, sticky="w")

        # Realtime-only: toggle the in-progress "live line" (transcript shown
        # while the speaker is still talking). Shares column 1 with adaptive
        # catch-up — the two are mutually exclusive by mode, so
        # _update_speed_button_states() shows at most one of them.
        self.show_interim_var = tk.BooleanVar(
            value=self._saved_settings.show_interim_transcript
        )
        self.show_interim_cb = self._checkbox(
            cb_row,
            "show_interim_transcript",
            self.show_interim_var,
            self._on_show_interim_change,
        )
        self.show_interim_cb.grid(row=0, column=1, sticky="w")

    def _create_advanced_card(self) -> None:
        card = self._section_card(
            self._col_right,
            "⚙",
            "advanced_settings",
            toggle_command=self._toggle_advanced_settings,
        )
        card.grid_columnconfigure(0, weight=1)
        self.advanced_card = card

        self.advanced_frame = ctk.CTkFrame(card, fg_color="transparent")
        self.advanced_frame.grid_columnconfigure(0, weight=1)

        # ── Translation section (the LLM that produces the translation) ───────
        translation_header = ctk.CTkLabel(
            self.advanced_frame,
            text=self.gui_texts.get("section_translation", "Translation"),
            font=ctk.CTkFont(family="Segoe UI", size=15, weight="bold"),
            text_color=self._colors["text"],
        )
        translation_header.grid(row=3, column=0, sticky="w", padx=18, pady=(6, 6))
        translation_header._text_key = "section_translation"  # type: ignore[attr-defined]
        self._section_titles.append(translation_header)

        provider_combo_row = ctk.CTkFrame(self.advanced_frame, fg_color="transparent")
        provider_combo_row.grid(row=4, column=0, sticky="ew", padx=18, pady=(0, 14))
        provider_combo_row.grid_columnconfigure(0, weight=1)
        self.provider_combo = self._combo(
            provider_combo_row,
            values=[],
            command=lambda _value: self._on_provider_change(),
        )
        self.provider_combo.grid(row=0, column=0, sticky="ew")
        self._refresh_translation_provider_combo()

        # ── Translation Model ─────────────────────────────────────────────────
        trans_combo_row = ctk.CTkFrame(self.advanced_frame, fg_color="transparent")
        trans_combo_row.grid(row=5, column=0, sticky="ew", padx=18, pady=(0, 14))
        trans_combo_row.grid_columnconfigure(0, weight=1)
        provider = self._saved_settings.ai_provider
        translation_choices = get_model_choices(provider, "translation")
        self._model_display_names = [name for name, _model_id in translation_choices]
        self._model_ids = [model_id for _name, model_id in translation_choices]
        self.model_combo = self._combo(
            trans_combo_row,
            values=self._model_display_names,
            command=lambda _value: self._on_model_change(),
        )
        default_translation = get_default_model(provider, "translation")
        if self._saved_settings.translation_model in self._model_ids:
            self.model_combo.current(
                self._model_ids.index(self._saved_settings.translation_model)
            )
        elif default_translation in self._model_ids:
            self.model_combo.current(self._model_ids.index(default_translation))
        else:
            self.model_combo.current(0)
        self.model_combo.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self.use_default_translation_var = tk.BooleanVar(
            value=self._saved_settings.use_default_translation_model
        )
        self.use_default_translation_cb = self._checkbox(
            trans_combo_row,
            "use_default",
            self.use_default_translation_var,
            self._on_use_default_translation_change,
        )
        self.use_default_translation_cb.grid(row=0, column=1, sticky="e")

        # ── Transcription section (speech-to-text engine) ─────────────────────
        # The provider list follows the Processing Strategy (in Translation
        # flow): real-time exposes the streaming engines (Deepgram/OpenAI);
        # chunk/semantic expose OpenAI/Gemini.
        transcription_header = ctk.CTkLabel(
            self.advanced_frame,
            text=self.gui_texts.get("section_transcription", "Transcription"),
            font=ctk.CTkFont(family="Segoe UI", size=15, weight="bold"),
            text_color=self._colors["text"],
        )
        transcription_header.grid(row=0, column=0, sticky="w", padx=18, pady=(4, 6))
        transcription_header._text_key = "section_transcription"  # type: ignore[attr-defined]
        self._section_titles.append(transcription_header)

        sc_provider_combo_row = ctk.CTkFrame(
            self.advanced_frame, fg_color="transparent"
        )
        sc_provider_combo_row.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 14))
        sc_provider_combo_row.grid_columnconfigure(0, weight=1)
        # Segmented engines (chunk/semantic) vs the streaming engines
        # (real-time). The active list is chosen by the Processing Strategy.
        self._segmented_transcription_provider_choices = [
            (name, pid)
            for name, pid in TRANSCRIPTION_PROVIDER_CHOICES
            if pid not in STREAMING_TRANSCRIPTION_PROVIDERS
        ]
        self._streaming_transcription_provider_choices = [
            ("Google Gemini", "gemini_realtime"),
            ("OpenAI", "openai_realtime"),
            ("Deepgram", "deepgram"),
        ]
        self._transcription_provider_display_names = []
        self._transcription_provider_ids = []
        self.transcription_provider_combo = self._combo(
            sc_provider_combo_row,
            values=[],
            command=lambda _value: self._on_transcription_provider_change(),
        )
        self.transcription_provider_combo.grid(row=0, column=0, sticky="ew")
        self._refresh_transcription_provider_combo()

        # ── Transcription Model ───────────────────────────────────────────────
        sc_provider = self._saved_settings.transcription_provider
        trans_sc_combo_row = ctk.CTkFrame(self.advanced_frame, fg_color="transparent")
        trans_sc_combo_row.grid(row=2, column=0, sticky="ew", padx=18, pady=(0, 14))
        trans_sc_combo_row.grid_columnconfigure(0, weight=1)
        transcription_choices = get_model_choices(sc_provider, "transcription")
        self._transcription_display_names = [
            name for name, _model_id in transcription_choices
        ]
        self._transcription_ids = [
            model_id for _name, model_id in transcription_choices
        ]
        self.transcription_combo = self._combo(
            trans_sc_combo_row,
            values=self._transcription_display_names,
            command=lambda _value: self._on_transcription_model_change(),
        )
        default_transcription = get_default_model(sc_provider, "transcription")
        if self._saved_settings.transcription_model in self._transcription_ids:
            self.transcription_combo.current(
                self._transcription_ids.index(self._saved_settings.transcription_model)
            )
        elif default_transcription in self._transcription_ids:
            self.transcription_combo.current(
                self._transcription_ids.index(default_transcription)
            )
        else:
            self.transcription_combo.current(0)
        self.transcription_combo.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self.use_default_transcription_var = tk.BooleanVar(
            value=self._saved_settings.use_default_transcription_model
        )
        self.use_default_transcription_cb = self._checkbox(
            trans_sc_combo_row,
            "use_default",
            self.use_default_transcription_var,
            self._on_use_default_transcription_change,
        )
        self.use_default_transcription_cb.grid(row=0, column=1, sticky="e")

        # ── Other Settings title ──────────────────────────────────────────────
        other_settings_label = ctk.CTkLabel(
            self.advanced_frame,
            text=self.gui_texts.get("other_settings", "Other Settings"),
            font=ctk.CTkFont(family="Segoe UI", size=15, weight="bold"),
            text_color=self._colors["text"],
        )
        other_settings_label.grid(row=6, column=0, sticky="w", padx=18, pady=(6, 6))
        other_settings_label._text_key = "other_settings"  # type: ignore[attr-defined]
        self._section_titles.append(other_settings_label)

        self.show_footer_var = tk.BooleanVar(value=self._saved_settings.show_footer)
        self.show_footer_checkbox = self._checkbox(
            self.advanced_frame,
            "show_footer",
            self.show_footer_var,
            self._on_show_footer_change,
        )
        self.show_footer_checkbox.grid(
            row=7, column=0, sticky="ew", padx=16, pady=(0, 8)
        )

        self.noise_filter_var = tk.BooleanVar(value=self._saved_settings.noise_filter)
        self.noise_filter_cb = self._checkbox(
            self.advanced_frame,
            "noise_filter",
            self.noise_filter_var,
            self._on_noise_filter_change,
        )
        self.noise_filter_cb.grid(row=8, column=0, sticky="ew", padx=16, pady=(0, 8))

        self.auto_cleanup_logs_var = tk.BooleanVar(
            value=self._saved_settings.auto_cleanup_logs
        )
        self.auto_cleanup_logs_cb = self._checkbox(
            self.advanced_frame,
            "auto_cleanup_logs",
            self.auto_cleanup_logs_var,
            self._on_auto_cleanup_logs_change,
        )
        self.auto_cleanup_logs_cb.grid(
            row=9, column=0, sticky="ew", padx=16, pady=(0, 8)
        )

        self.auto_cleanup_content_var = tk.BooleanVar(
            value=self._saved_settings.auto_cleanup_content
        )
        self.auto_cleanup_content_cb = self._checkbox(
            self.advanced_frame,
            "auto_cleanup_content",
            self.auto_cleanup_content_var,
            self._on_auto_cleanup_content_change,
        )
        self.auto_cleanup_content_cb.grid(
            row=10, column=0, sticky="ew", padx=16, pady=(0, 8)
        )

        self.hide_subtitle_on_stop_var = tk.BooleanVar(
            value=self._saved_settings.hide_subtitle_on_stop
        )
        self.hide_subtitle_on_stop_cb = self._checkbox(
            self.advanced_frame,
            "hide_subtitle_on_stop",
            self.hide_subtitle_on_stop_var,
            self._on_hide_subtitle_on_stop_change,
        )
        self.hide_subtitle_on_stop_cb.grid(
            row=11, column=0, sticky="ew", padx=16, pady=(0, 8)
        )

        self.auto_start_var = tk.BooleanVar(value=self._saved_settings.auto_start)
        self.auto_start_cb = self._checkbox(
            self.advanced_frame,
            "auto_start_on_launch",
            self.auto_start_var,
            self._on_auto_start_change,
        )
        self.auto_start_cb.grid(row=12, column=0, sticky="ew", padx=16, pady=(0, 8))

        self.auto_stop_inactivity_var = tk.BooleanVar(
            value=self._saved_settings.auto_stop_inactivity
        )
        self.auto_stop_inactivity_cb = self._checkbox(
            self.advanced_frame,
            "auto_stop_inactivity",
            self.auto_stop_inactivity_var,
            self._on_auto_stop_inactivity_change,
        )
        self.auto_stop_inactivity_cb.grid(
            row=13, column=0, sticky="ew", padx=16, pady=(0, 8)
        )

        self.always_on_top_var = tk.BooleanVar(
            value=self._saved_settings.always_on_top
        )
        self.always_on_top_cb = self._checkbox(
            self.advanced_frame,
            "always_on_top",
            self.always_on_top_var,
            self._on_always_on_top_change,
        )
        self.always_on_top_cb.grid(
            row=14, column=0, sticky="ew", padx=16, pady=(0, 12)
        )

        self._sync_advanced_enabled_states()
        self._update_speed_button_states()
        self._align_provider_combo_widths()

    def _align_provider_combo_widths(self) -> None:
        """Match each provider dropdown's width to the model dropdown below it.
        The model rows reserve space on the right for a "Use Default" checkbox
        (combo padx (0, 10) + the checkbox); the provider rows have no checkbox,
        so pad them by the same amount to line the right edges up."""
        self.advanced_frame.update_idletasks()
        for combo, checkbox in (
            (self.provider_combo, self.use_default_translation_cb),
            (self.transcription_provider_combo, self.use_default_transcription_cb),
        ):
            combo.grid_configure(padx=(0, checkbox.winfo_reqwidth() + 10))

    def _create_log_panel(self) -> None:
        top_bar = ctk.CTkFrame(self.content, fg_color="transparent")
        top_bar.grid(row=0, column=0, sticky="ew", pady=(0, 18))
        top_bar.grid_columnconfigure(0, weight=1)
        top_bar.grid_columnconfigure(1, weight=0)

        self.logs_label = ctk.CTkLabel(
            top_bar,
            text=f"▤  {self.gui_texts.get('logs', 'Logs')}",
            font=ctk.CTkFont(size=28, weight="bold"),
            text_color=self._colors["text"],
        )
        self.logs_label.grid(row=0, column=0, sticky="w")
        self._labels.append(self.logs_label)

        self.right_status = ctk.CTkLabel(
            top_bar,
            text=self.gui_texts.get("stopped", "Ready"),
            font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
            text_color=self._colors["accent"],
            fg_color=self._colors["accent_soft"],
            corner_radius=999,
            width=150,
            height=42,
        )
        self.right_status.grid(row=0, column=1, sticky="e")

        self.log_panel = ctk.CTkFrame(
            self.content,
            fg_color=self._colors["panel"],
            border_color=self._colors["border"],
            border_width=1,
            corner_radius=24,
        )
        self.log_panel.grid(row=1, column=0, sticky="nsew")
        self.log_panel.grid_columnconfigure(0, weight=1)
        self.log_panel.grid_rowconfigure(0, weight=1)
        self._main_panels.append(self.log_panel)

        self.log_text = ctk.CTkTextbox(
            self.log_panel,
            corner_radius=20,
            border_width=1,
            border_color=self._colors["border"],
            fg_color=self._colors["log_bg"],
            text_color=self._colors["log_text"],
            font=ctk.CTkFont(family="Consolas", size=14),
            wrap="word",
        )
        self.log_text.grid(row=0, column=0, sticky="nsew", padx=18, pady=18)
        self.log_text.configure(state="disabled")

    def _subtitle_output_is_enabled(self) -> bool:
        """Whether the audience overlay window should exist at all."""
        return bool(getattr(self._saved_settings, "subtitle_output_enabled", True))

    def _create_subtitle_window(self) -> None:
        if not self._subtitle_output_is_enabled():
            # Run transcription/translation with no overlay window at all.
            return
        current_screen_idx = self.screen_combo.current()
        if current_screen_idx is None or current_screen_idx < 0:
            current_screen_idx = self.selected_screen_index
        self.selected_screen_index = current_screen_idx

        self.subtitle_window = SubtitleWindow(
            self,
            on_close=self.on_close,
            monitor_index=current_screen_idx,
            font_size_base=self._saved_settings.font_size_base,
            target_language=self._saved_settings.target_language,
            subtitle_mode=self._effective_subtitle_mode(),
            scroll_speed=self.speed_value,
            transparent_static=self._saved_settings.transparent_static,
            window_height_percent=self._saved_settings.window_height_percent,
            show_footer=self._saved_settings.show_footer,
            adaptive_catchup=self._saved_settings.adaptive_subtitle_catchup,
            bilingual_mode=self._saved_settings.bilingual_mode,
            theme_mode=getattr(
                self._saved_settings, "subtitle_theme_mode", self._theme_mode
            ),
            always_on_top=self._saved_settings.always_on_top,
            on_stop=self._request_stop_from_subtitle,
        )
        self.height_slider.set(self._saved_settings.window_height_percent)
        if not self._running:
            # Window kept open while stopped (default setup): tell the
            # audience the missing subtitles are deliberate. Cleared on Start.
            self.subtitle_window.set_stopped_hint(True)
        # An 'until stopped' announcement survives window recreation — re-draw
        # it on the fresh overlay.
        self._apply_active_announcement()
        # The control panel now has an overlay to float above.
        self._apply_control_window_topmost()

    def _destroy_subtitle_window(self) -> None:
        try:
            if self.subtitle_window and self.subtitle_window.winfo_exists():
                self.subtitle_window.destroy()
        except Exception as exc:
            log(f"Error destroying subtitle window: {exc}", level="DEBUG")
        self.subtitle_window = None
        # No overlay left to float above → drop the control panel's topmost.
        self._apply_control_window_topmost()

    def _finalize_setup(self) -> None:
        self._set_status(False)
        # Track window focus from startup so the very first dropdown click
        # after the window regains focus only restores focus (opens on the
        # second click). Otherwise this is installed lazily on the first
        # dropdown open, leaving the first-ever interaction unguarded.
        CustomDropdown._install_global_handler(self)
        self._load_api_key_on_startup()
        self._update_speed_button_states()
        if self._saved_settings.hide_subtitle_on_stop:
            self.after(150, self._destroy_subtitle_window)
        self._start_log_polling()
        self.translation_poll_job = self.after(50, self._process_translation_queue)
        self.error_poll_job = self.after(250, self._poll_errors)
        self.after(300, lambda: self._setup_autohide_scrollbar(self.sidebar))
        self._start_update_check()
        log(self.gui_texts.get("stopped", "Ready"), level="INFO")
        if self._saved_settings.auto_start:
            self.after(700, self.on_start)

    def _get_screen_names(self) -> list[str]:
        monitors = get_monitors()
        return [
            f"{idx + 1}: {monitor.width}x{monitor.height} @ {monitor.x},{monitor.y}"
            for idx, monitor in enumerate(monitors)
        ]

    def _load_api_key_on_startup(self) -> None:
        """Load stored API keys; prompt if the active provider has none.

        The OpenAI key is loaded whenever available — even under another
        provider it still serves the RAG query embeddings. The Gemini client
        loads its own key lazily on first use.
        """
        openai_key = (get_stored_api_key("openai") or "").strip()
        if openai_key:
            set_api_key(openai_key)

        if has_usable_key(self._saved_settings.ai_provider):
            return
        # No key found → prompt after the window is fully drawn
        self.after(500, self.on_change_key)

    def _append_log_line(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{text}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _poll_logs(self) -> None:
        if not self._log_polling:
            return
        try:
            while not log_queue.empty():
                self._append_log_line(log_queue.get_nowait())
        except Exception as exc:
            log(f"Log polling error: {exc}", level="DEBUG")
        self.log_poll_job = self.after(100, self._poll_logs)

    def _start_log_polling(self) -> None:
        if self._log_polling:
            return
        self._log_polling = True
        self._poll_logs()

    def _process_translation_queue(self) -> None:
        try:
            batch_size, next_poll_ms = self._get_translation_drain_policy()
            processed = 0
            while (
                processed < batch_size and not self.controller.translation_queue.empty()
            ):
                text, source_text = self.translation_queue.get_nowait()
                if self.subtitle_window and self.subtitle_window.winfo_exists():
                    self.subtitle_window.add_subtitle(text, source_text=source_text)
                processed += 1
            self._update_live_transcript_display()
        except Exception as exc:
            log(f"Translation queue processing error: {exc}", level="DEBUG")
            next_poll_ms = 100
        self.translation_poll_job = self.after(
            next_poll_ms, self._process_translation_queue
        )

    def _update_live_transcript_display(self) -> None:
        """Mirror the controller's in-progress streaming transcript onto the
        subtitle window's live line (settled subtitles are drained first, so
        a translation and its live-line removal land in the same tick).
        The window only renders it in Realtime mode."""
        if self._saved_settings.pipeline_mode != PIPELINE_MODE_STREAMING:
            return
        if self.subtitle_window and self.subtitle_window.winfo_exists():
            if self._saved_settings.show_interim_transcript:
                text, settled = self.controller.get_live_transcript()
            else:
                # Live line off: keep the window's live line cleared so only
                # settled translation blocks show.
                text, settled = "", False
            self.subtitle_window.set_live_text(text, settled)

    def _poll_errors(self) -> None:
        while True:
            try:
                error = self.error_queue.get_nowait()
            except queue.Empty:
                break
            try:
                if str(error).startswith("audio_device_lost:"):
                    self._handle_audio_device_lost()
                else:
                    log(f"Controller error: {error}", level="ERROR")
            except Exception as exc:
                log(f"Error handling controller error: {exc}", level="ERROR")
        self.error_poll_job = self.after(1000, self._poll_errors)

    def _handle_audio_device_lost(self) -> None:
        if self._running:
            self.on_stop()
        log(self.gui_texts.get("audio_device_lost", "Audio device lost"), level="ERROR")
        self._alert(
            self.gui_texts.get("audio_device_lost", "Audio device lost"),
            self.gui_texts.get("audio_device_lost", "Audio device lost"),
            parent=self,
        )

    def _get_translation_drain_policy(self) -> tuple[int, int]:
        queue_depth = self.controller.translation_queue.qsize()
        mode = self._saved_settings.subtitle_mode
        adaptive = self._saved_settings.adaptive_subtitle_catchup
        batch_size = 1
        # 50 ms base so the live streaming transcript line (mirrored on every
        # tick) redraws twice as often — the growing/self-correcting text
        # appears sooner and reads smoother. Cheap: set_live_text no-ops when
        # nothing changed.
        next_poll_ms = 50

        if not adaptive or mode != SUBTITLE_MODE_CONTINUOUS:
            return batch_size, next_poll_ms
        if queue_depth >= 20:
            return 4, 50
        if queue_depth >= 10:
            return 3, 65
        if queue_depth >= 5:
            return 2, 80
        if (
            mode == SUBTITLE_MODE_CONTINUOUS
            and self.subtitle_window
            and self.subtitle_window.winfo_exists()
        ):
            visual_backlog = self.subtitle_window.get_subtitle_backlog_count()
            if visual_backlog >= 8:
                return 2, 70
            if visual_backlog >= 4:
                return 1, 80
        return batch_size, next_poll_ms

    def on_change_key(self, provider: str | None = None) -> None:
        prompt_for_api_key(
            root=self,
            startup=False,
            on_close=lambda: None,
            colors=self._colors,
            texts=self.gui_texts,
            provider=provider or self._saved_settings.ai_provider,
        )

    def _required_key_providers(self) -> list[str]:
        return required_key_providers(self._saved_settings)

    def on_start(self) -> None:
        # Prompt for any missing key; if the user dismisses the dialog the app
        # stays stopped (the dialog re-opens on the next Start attempt).
        for provider in self._required_key_providers():
            if not has_usable_key(provider):
                self._prompt_provider_key(provider)
                if not has_usable_key(provider):
                    return
        try:
            self.controller.start(input_device=self.get_selected_device_index())
        except Exception as exc:
            self._alert(
                self.gui_texts.get("error_start_failed", "Start failed"),
                str(exc),
                parent=self,
                danger=True,
            )
            return
        self._running = True
        self._refresh_provider_combos()  # hide keyless providers while running
        self._set_status(True)
        self._sync_advanced_enabled_states()
        self._start_log_polling()
        self._schedule_inactivity_check()
        if self._saved_settings.hide_subtitle_on_stop and (
            not self.subtitle_window or not self.subtitle_window.winfo_exists()
        ):
            self._create_subtitle_window()
        if self.subtitle_window and self.subtitle_window.winfo_exists():
            self.subtitle_window.set_stopped_hint(False)
        log(self.gui_texts.get("log_started", "Started."), level="INFO")

    def _request_stop_from_subtitle(self) -> None:
        """Esc on the subtitle overlay stops the pipeline (like the Stop
        button) and never closes the window or the app. Ignored when nothing
        is running, so a stray Esc while idle does nothing."""
        if self._running:
            self.on_stop()

    def on_stop(self) -> None:
        try:
            self.controller.stop()
        except Exception as exc:
            self._alert(
                self.gui_texts.get("error_stop_failed", "Stop failed"),
                str(exc),
                parent=self,
                danger=True,
            )
            return
        self._running = False
        self._refresh_provider_combos()  # restore the full provider list
        self._set_status(False)
        self._sync_advanced_enabled_states()
        self._cancel_inactivity_check()
        # Keep the overlay alive if an 'until stopped' announcement is showing —
        # it must survive a translation stop (user decision). Stopping the
        # announcement itself then closes the overlay if hide-on-stop is set.
        if (
            self._saved_settings.hide_subtitle_on_stop
            and not self._has_active_announcement()
        ):
            self._destroy_subtitle_window()
        elif self.subtitle_window and self.subtitle_window.winfo_exists():
            self.subtitle_window.set_stopped_hint(True)
        log(self.gui_texts.get("log_stopped", "Stopped."), level="INFO")

    # ── Inactivity auto-stop ────────────────────────────────────────────────
    # Cost guard for forgotten sessions: while running, poll the controller's
    # last-transcription timestamp and stop when nothing arrived for
    # AUTO_STOP_INACTIVITY_SECONDS (checkbox in Advanced, default on).

    def _schedule_inactivity_check(self) -> None:
        self._cancel_inactivity_check()
        self.inactivity_check_job = self.after(
            15_000, self._check_inactivity_auto_stop
        )

    def _cancel_inactivity_check(self) -> None:
        if self.inactivity_check_job is not None:
            try:
                self.after_cancel(self.inactivity_check_job)
            except Exception:
                pass
            self.inactivity_check_job = None

    def _check_inactivity_auto_stop(self) -> None:
        self.inactivity_check_job = None
        if not self._running:
            return
        if (
            self._saved_settings.auto_stop_inactivity
            and self.controller.seconds_since_last_activity()
            >= AUTO_STOP_INACTIVITY_SECONDS
        ):
            log(
                "Auto-stop: no transcription for "
                f"{AUTO_STOP_INACTIVITY_SECONDS // 60} minutes — stopping.",
                level="INFO",
            )
            self.on_stop()
            return
        self._schedule_inactivity_check()

    def _set_status(self, running: bool) -> None:
        if running:
            self.start_btn.configure(
                state="disabled",
                fg_color=self._colors["button"],
                hover_color=self._colors["button_hover"],
                text_color_disabled=self._colors["muted"],
            )
            self.stop_btn.configure(
                state="normal",
                fg_color=self._colors["danger"],
                hover_color=self._colors["danger_hover"],
                text_color="#ffffff",
            )
            running_text = self._clean_action_label("running")
            self.status_label.configure(text=running_text, text_color="#ffffff")
            self.status_badge.configure(fg_color=self._colors["accent"])
            self.right_status.configure(
                text=running_text,
                fg_color=self._colors["accent"],
                text_color="#ffffff",
            )
            self.strategy_running_hint.configure(text_color=self._colors["warning"])
            self.strategy_running_hint.grid()
        else:
            self.start_btn.configure(
                state="normal",
                fg_color=self._colors["accent"],
                hover_color=self._colors["accent_hover"],
                text_color="#ffffff",
            )
            self.stop_btn.configure(
                state="disabled",
                fg_color=self._colors["button"],
                hover_color=self._colors["button_hover"],
                text_color_disabled=self._colors["muted"],
            )
            stopped_text = self.gui_texts.get("stopped", "Stopped")
            self.status_label.configure(
                text=stopped_text, text_color=self._colors["danger"]
            )
            self.status_badge.configure(fg_color=self._colors["danger_soft"])
            self.right_status.configure(
                text=stopped_text,
                fg_color=self._colors["danger_soft"],
                text_color=self._colors["danger"],
            )
            self.strategy_running_hint.grid_remove()

    def _get_input_devices(self) -> tuple[list[str], list[str], list[int], list[bool]]:
        return get_input_devices()

    def get_selected_device_index(self) -> int | None:
        idx = self.device_combo.current()
        if idx is None or idx < 0 or idx >= len(self.device_indices):
            return None
        return self.device_indices[idx]

    def _selected_device_loopback(self) -> bool:
        idx = self.device_combo.current()
        if idx is None or idx < 0 or idx >= len(self.device_loopback_flags):
            return False
        return self.device_loopback_flags[idx]

    def _on_device_change(self) -> None:
        selection = self.device_combo.current()
        if selection is not None and 0 <= selection < len(self.device_base_names):
            self._saved_settings.input_device_name = self.device_base_names[selection]
            if self._running:
                self.controller.change_input_device(self.device_indices[selection])
            log(f"Input device: {self.device_base_names[selection]}", level="INFO")
        self._save_current_settings()

    def _on_screen_change(self) -> None:
        idx = self.screen_combo.current()
        if idx is None or idx < 0:
            return
        self.selected_screen_index = idx
        if self.subtitle_window and self.subtitle_window.winfo_exists():
            self.subtitle_window.set_monitor(idx)
        self._saved_settings.monitor_index = idx
        log(f"Subtitle screen: {self.screen_combo.get()}", level="INFO")
        self._save_current_settings()

    def _on_language_change(self) -> None:
        canonical = language_canonical_name(self.language_combo.get())
        self._saved_settings.target_language = canonical
        if self.subtitle_window and self.subtitle_window.winfo_exists():
            self.subtitle_window.set_language(canonical)
        log(f"Target language: {canonical}", level="INFO")
        self._save_current_settings()

    def _on_source_language_change(self) -> None:
        canonical = language_canonical_name(self.source_lang_combo.get())
        self._saved_settings.source_language = canonical
        log(f"Source language: {canonical}", level="INFO")
        self._save_current_settings()
        # Segmented mode re-reads the source language per audio segment; the
        # streaming (Deepgram) socket fixes it at connect, so reconnect to apply.
        self._restart_pipeline_for_live_change()

    def _restart_pipeline_for_live_change(self) -> None:
        """Reconnect the streaming pipeline so a change that can't apply on a
        live Deepgram socket (source language, transcription model) takes
        effect immediately. No-op unless a stream is actually running — in
        segmented mode these settings are re-read per segment. Expect a brief
        audio gap, same as a manual Stop → Start."""
        if not self._running:
            return
        if self._saved_settings.pipeline_mode != PIPELINE_MODE_STREAMING:
            return
        log("Restarting live stream to apply change…", level="INFO")
        try:
            self.controller.restart(input_device=self.get_selected_device_index())
        except Exception as exc:
            # start() may fail after stop() already ran → reflect stopped state.
            self._running = False
            self._refresh_provider_combos()  # restore the full provider list
            self._set_status(False)
            self._sync_advanced_enabled_states()
            self._alert(
                self.gui_texts.get("error_start_failed", "Start failed"),
                str(exc),
                parent=self,
                danger=True,
            )
            return
        log("Live stream restarted", level="INFO")

    def _refresh_source_language_combo(self) -> None:
        """Real-time (streaming) mode can't auto-detect the source language, so
        'Automatic' is removed from the picker whenever streaming is active. If
        the stored source was 'Automatic', fall back to the first real language
        (Arabic, the primary use case)."""
        if not hasattr(self, "source_lang_combo"):
            return
        streaming = self._saved_settings.pipeline_mode == PIPELINE_MODE_STREAMING
        if streaming:
            choices = [n for n in self._source_lang_names if n != "Automatic"]
        else:
            choices = list(self._source_lang_names)
        self.source_lang_combo.configure(
            values=[language_display_name(n) for n in choices]
        )
        if streaming and self._saved_settings.source_language == "Automatic":
            default_src = choices[0] if choices else "Arabic"
            self._saved_settings.source_language = default_src
            self.source_lang_combo.set(language_display_name(default_src))
            log(
                f"Source language set to {default_src} "
                "(real-time mode has no auto-detect)",
                level="INFO",
            )
        else:
            self.source_lang_combo.set(
                language_display_name(self._saved_settings.source_language)
            )

    def _on_swap_languages(self) -> None:
        source = language_canonical_name(self.source_lang_combo.get())
        target = language_canonical_name(self.language_combo.get())
        if target not in self._source_lang_names or source not in TARGET_LANGUAGE_NAMES:
            return
        self.source_lang_combo.set(language_display_name(target))
        self.language_combo.set(language_display_name(source))
        self._on_source_language_change()
        self._on_language_change()

    def _subtitle_mode_choices(self) -> list[str]:
        return subtitle_mode_choices(self._saved_settings)

    def _effective_subtitle_mode(self) -> str:
        return effective_subtitle_mode(self._saved_settings)

    def _refresh_subtitle_mode_combo(self) -> None:
        """Rebuild the Subtitles dropdown for the current strategy and select
        the effective mode."""
        self._subtitle_mode_values = self._subtitle_mode_choices()
        self.subtitle_mode_combo.configure(
            values=[self._mode_label(m) for m in self._subtitle_mode_values]
        )
        effective = self._effective_subtitle_mode()
        if effective in self._subtitle_mode_values:
            self.subtitle_mode_combo.current(
                self._subtitle_mode_values.index(effective)
            )
        else:
            self.subtitle_mode_combo.current(0)

    def _apply_effective_subtitle_mode(self) -> None:
        """Sync the window and the Subtitles dropdown to the effective mode
        after anything that can change it (strategy switch, mode switch)."""
        self._refresh_subtitle_mode_combo()
        effective = self._effective_subtitle_mode()
        window = getattr(self, "subtitle_window", None)
        if window and window.winfo_exists():
            if window.get_subtitle_mode() != effective:
                window.set_subtitle_mode(effective)
        self._update_speed_button_states()

    def _on_subtitle_mode_change(self) -> None:
        selection = self.subtitle_mode_combo.current()
        if selection is None or not (0 <= selection < len(self._subtitle_mode_values)):
            return
        mode = self._subtitle_mode_values[selection]
        self._saved_settings.subtitle_mode = mode
        if self.subtitle_window and self.subtitle_window.winfo_exists():
            self.subtitle_window.set_subtitle_mode(mode)
        self._update_speed_button_states()
        log(f"Subtitle mode: {self._mode_label(mode)}", level="INFO")
        self._save_current_settings()

    def _increase_subtitle_font(self) -> None:
        if self.subtitle_window and self.subtitle_window.winfo_exists():
            self.subtitle_window.increase_font()
            self._saved_settings.font_size_base = (
                self.subtitle_window.get_font_size_base()
            )
            log(
                f"Font size changed to: {self.subtitle_window.get_current_font_size()}",
                level="INFO",
            )
            self._save_current_settings()

    def _decrease_subtitle_font(self) -> None:
        if self.subtitle_window and self.subtitle_window.winfo_exists():
            self.subtitle_window.decrease_font()
            self._saved_settings.font_size_base = (
                self.subtitle_window.get_font_size_base()
            )
            log(
                f"Font size changed to: {self.subtitle_window.get_current_font_size()}",
                level="INFO",
            )
            self._save_current_settings()

    def _on_height_slider_change(self, value: float) -> None:
        percent = int(round(value))
        self.height_value_label.configure(text=f"{percent}%")
        if self.height_apply_job:
            self.after_cancel(self.height_apply_job)
        self.height_apply_job = self.after(
            120, lambda: self._apply_height_change(percent)
        )

    def _apply_height_change(self, percent: int) -> None:
        self.height_apply_job = None
        if self.subtitle_window and self.subtitle_window.winfo_exists():
            self.subtitle_window.set_window_height_percent(percent)
        self._saved_settings.window_height_percent = percent
        log(f"Subtitle height: {percent}%", level="INFO")
        self._save_current_settings()

    def _increase_scroll_speed(self) -> None:
        if self.subtitle_window and self.subtitle_window.winfo_exists():
            self.speed_value = self.subtitle_window.increase_scroll_speed()
        else:
            self.speed_value = min(5.0, round(self.speed_value + 0.5, 1))
        self._saved_settings.scroll_speed = self.speed_value
        self.speed_label.configure(text=f"{self.speed_value:.1f}x")
        log(f"Scroll speed: {self.speed_value:.1f}x", level="INFO")
        self._save_current_settings()

    def _decrease_scroll_speed(self) -> None:
        if self.subtitle_window and self.subtitle_window.winfo_exists():
            self.speed_value = self.subtitle_window.decrease_scroll_speed()
        else:
            self.speed_value = max(0.5, round(self.speed_value - 0.5, 1))
        self._saved_settings.scroll_speed = self.speed_value
        self.speed_label.configure(text=f"{self.speed_value:.1f}x")
        log(f"Scroll speed: {self.speed_value:.1f}x", level="INFO")
        self._save_current_settings()

    def _on_transparent_change(self) -> None:
        self._saved_settings.transparent_static = self.transparent_var.get()
        if self.transparent_var.get():
            self.height_slider.set(100)
            self._apply_height_change(100)
        if self.subtitle_window and self.subtitle_window.winfo_exists():
            self.subtitle_window.set_transparent_static(self.transparent_var.get())
        log(
            f"Transparent background: {'on' if self.transparent_var.get() else 'off'}",
            level="INFO",
        )
        self._save_current_settings()

    def _on_show_footer_change(self) -> None:
        self._saved_settings.show_footer = self.show_footer_var.get()
        if self.subtitle_window and self.subtitle_window.winfo_exists():
            self.subtitle_window.set_show_footer(self.show_footer_var.get())
        log(
            f"Show footer: {'on' if self.show_footer_var.get() else 'off'}",
            level="INFO",
        )
        self._save_current_settings()

    def _on_bilingual_change(self) -> None:
        self._saved_settings.bilingual_mode = self.bilingual_var.get()
        if self.subtitle_window and self.subtitle_window.winfo_exists():
            self.subtitle_window.set_bilingual_mode(self.bilingual_var.get())
        log(
            f"Bilingual mode: {'on' if self.bilingual_var.get() else 'off'}",
            level="INFO",
        )
        self._save_current_settings()

    def _on_show_interim_change(self) -> None:
        # Realtime mode only. Off => the live in-progress line is suppressed;
        # the feed shows only finished translation blocks. Takes effect live:
        # the next _update_live_transcript_display tick pushes the (possibly
        # empty) live text; call it now so toggling clears/restores at once.
        self._saved_settings.show_interim_transcript = self.show_interim_var.get()
        self._update_live_transcript_display()
        log(
            f"Live transcript: {'on' if self.show_interim_var.get() else 'off'}",
            level="INFO",
        )
        self._save_current_settings()

    def _on_adaptive_catchup_change(self) -> None:
        self._saved_settings.adaptive_subtitle_catchup = self.adaptive_catchup_var.get()
        if self.subtitle_window and self.subtitle_window.winfo_exists():
            self.subtitle_window.set_adaptive_catchup(self.adaptive_catchup_var.get())
        log(
            f"Adaptive catchup: {'on' if self.adaptive_catchup_var.get() else 'off'}",
            level="INFO",
        )
        self._save_current_settings()

    def _on_noise_filter_change(self) -> None:
        # Takes effect live: the segmented pipeline and the streaming feeder
        # both re-read the (cached) setting per segment/chunk.
        self._saved_settings.noise_filter = self.noise_filter_var.get()
        log(
            f"Noise filter: {'on' if self.noise_filter_var.get() else 'off'}",
            level="INFO",
        )
        self._save_current_settings()

    def _on_auto_cleanup_logs_change(self) -> None:
        self._saved_settings.auto_cleanup_logs = self.auto_cleanup_logs_var.get()
        log(
            f"Auto cleanup logs: {'on' if self.auto_cleanup_logs_var.get() else 'off'}",
            level="INFO",
        )
        self._save_current_settings()

    def _on_auto_cleanup_content_change(self) -> None:
        self._saved_settings.auto_cleanup_content = self.auto_cleanup_content_var.get()
        log(
            "Auto cleanup content: "
            f"{'on' if self.auto_cleanup_content_var.get() else 'off'}",
            level="INFO",
        )
        self._save_current_settings()

    def _on_auto_stop_inactivity_change(self) -> None:
        # Read live by the running check loop — no restart needed.
        self._saved_settings.auto_stop_inactivity = self.auto_stop_inactivity_var.get()
        log(
            "Auto-stop on inactivity: "
            f"{'on' if self.auto_stop_inactivity_var.get() else 'off'}",
            level="INFO",
        )
        self._save_current_settings()

    def _on_auto_start_change(self) -> None:
        self._saved_settings.auto_start = self.auto_start_var.get()
        log(
            f"Auto-start on launch: {'on' if self.auto_start_var.get() else 'off'}",
            level="INFO",
        )
        self._save_current_settings()

    def _sync_advanced_enabled_states(self) -> None:
        """Single source of truth for the enabled/disabled state of every
        widget in the Advanced card.

        Called after any provider/model/strategy change and on start/stop.
        Rules: translation + segmented-transcription provider/model dropdowns
        stay changeable at runtime (the pipeline re-reads them per translation
        / audio segment), each greyed only when its "Use Default" box is
        ticked; streaming (Deepgram) transcription is a live stream and locks
        while running; the transcription model is locked to Nova-3 under
        Deepgram; the Processing Strategy is the master switch (greyed only
        while running or pinned to the default, never merely because streaming
        is active — it's how the user leaves streaming).
        """
        running = self._running
        # Translation provider + model: changeable at runtime — translation
        # runs through translate_text() per utterance, which re-reads the
        # provider/model chain on every call (both segmented and streaming).
        # Only "Use Default" locks BOTH (pinned to the provider's default).
        if self._saved_settings.use_default_translation_model:
            self.provider_combo.configure(state="disabled")
            self.model_combo.configure(state="disabled")
        else:
            self.provider_combo.configure(state="readonly")
            self.model_combo.configure(state="readonly")
        self.use_default_translation_cb.configure(state="normal")

        # Transcription provider — greyed ONLY when "Use Default" is on, the
        # same rule as the model and the Translation section (consistency, and
        # it stops looking "broken" while running). Today real-time lists only
        # Deepgram, so there's nothing else to pick; more streaming providers
        # added later slot in here and follow the same enable rule.
        if self._saved_settings.use_default_transcription_model:
            self.transcription_provider_combo.configure(state="disabled")
        else:
            self.transcription_provider_combo.configure(state="readonly")

        # Transcription model — changeable at runtime in BOTH modes.
        # - Segmented (OpenAI/Gemini): re-read per audio segment.
        # - Streaming (Deepgram, Nova-3/Nova-2): the socket is opened with one
        #   fixed model, so a change while running transparently reconnects the
        #   stream (_restart_pipeline_for_live_change) to apply it.
        # Only "Use Default" locks it (always-clickable checkbox, like
        # Translation's).
        self.use_default_transcription_cb.configure(state="normal")
        if self._saved_settings.use_default_transcription_model:
            self.transcription_combo.configure(state="disabled")
        else:
            self.transcription_combo.configure(state="readonly")

        # Processing strategy (master switch — realtime/chunk/semantic).
        # Locked only while running; NOT greyed under streaming — this
        # dropdown is how the user switches out of streaming.
        if running:
            self.strategy_combo.configure(state="disabled")
        else:
            self.strategy_combo.configure(state="readonly")

        # Running hint under the strategy row. It grows the right column; grid
        # auto-restretches the left column to match (equal-height columns), so
        # no manual re-levelling is needed.
        if running:
            self.strategy_running_hint.grid()
        else:
            self.strategy_running_hint.grid_remove()

    def _prompt_provider_key(self, provider: str) -> None:
        """Open the API-key dialog for a specific provider."""
        prompt_for_api_key(
            root=self,
            startup=False,
            on_close=lambda: None,
            colors=self._colors,
            texts=self.gui_texts,
            provider=provider,
        )

    def _strategy_labels(self) -> list[str]:
        """Localized display names for the Processing Strategy dropdown, in
        the order of ``self._strategy_ids`` (realtime, semantic, chunk)."""
        return [
            self.gui_texts.get("strategy_realtime", "Real-time streaming"),
            self.gui_texts.get("strategy_semantic", "Semantic buffering"),
            self.gui_texts.get("strategy_chunk", "Chunk-based"),
        ]

    def _current_strategy_index(self) -> int:
        return current_strategy_index(self._saved_settings)

    def _visible_provider_choices(
        self, choices: list[tuple[str, str]]
    ) -> list[tuple[str, str]]:
        return visible_provider_choices(choices, self._running)

    def _refresh_provider_combos(self) -> None:
        """Re-filter BOTH provider dropdowns for the current running state
        (called on every start/stop transition)."""
        self._refresh_translation_provider_combo()
        self._refresh_transcription_provider_combo()

    def _refresh_translation_provider_combo(self) -> None:
        """Populate the translation-provider dropdown (key-filtered while
        running, see _visible_provider_choices), preserving the selection."""
        choices = self._visible_provider_choices(list(PROVIDER_CHOICES))
        self._provider_display_names = [n for n, _p in choices]
        self._provider_ids = [p for _n, p in choices]
        self.provider_combo.configure(values=self._provider_display_names)
        ap = self._saved_settings.ai_provider
        if ap in self._provider_ids:
            self.provider_combo.current(self._provider_ids.index(ap))
        else:
            self.provider_combo.current(0)
            self._saved_settings.ai_provider = self._provider_ids[0]

    def _refresh_transcription_provider_combo(self) -> None:
        """Populate the transcription-provider dropdown for the current mode:
        the streaming engines (Deepgram/OpenAI) under real-time streaming;
        OpenAI/Gemini otherwise. Key-filtered while running (see
        _visible_provider_choices)."""
        if (
            self._saved_settings.transcription_provider
            in STREAMING_TRANSCRIPTION_PROVIDERS
        ):
            choices = self._streaming_transcription_provider_choices
        else:
            choices = self._segmented_transcription_provider_choices
        choices = self._visible_provider_choices(list(choices))
        self._transcription_provider_display_names = [n for n, _pid in choices]
        self._transcription_provider_ids = [pid for _n, pid in choices]
        self.transcription_provider_combo.configure(
            values=self._transcription_provider_display_names
        )
        tp = self._saved_settings.transcription_provider
        if tp in self._transcription_provider_ids:
            self.transcription_provider_combo.current(
                self._transcription_provider_ids.index(tp)
            )
        else:
            self.transcription_provider_combo.current(0)
            self._saved_settings.transcription_provider = (
                self._transcription_provider_ids[0]
            )

    def _refresh_transcription_model_combo(self, reset_default: bool) -> None:
        """Repopulate the transcription-model dropdown for the current
        transcription provider. Deepgram offers Nova-3 (default) and Nova-2,
        selected like any other provider's models."""
        provider = self._saved_settings.transcription_provider
        choices = get_model_choices(provider, "transcription")
        self._transcription_display_names = [name for name, _mid in choices]
        self._transcription_ids = [mid for _name, mid in choices]
        self.transcription_combo.configure(values=self._transcription_display_names)
        if reset_default:
            self.use_default_transcription_var.set(True)
            # The caller (strategy/provider switch) already set the provider;
            # only re-pin the model, don't re-reset the provider underneath it.
            self._on_use_default_transcription_change(save=False, reset_provider=False)
        else:
            # Keep "Use Default" as-is (a manual provider switch shouldn't
            # re-lock a customized setup); fall back to the provider default
            # when the stored model doesn't belong to the new provider.
            tm = self._saved_settings.transcription_model
            if tm in self._transcription_ids:
                idx = self._transcription_ids.index(tm)
            else:
                default_model = get_default_model(provider, "transcription")
                idx = (
                    self._transcription_ids.index(default_model)
                    if default_model in self._transcription_ids
                    else 0
                )
            self.transcription_combo.current(idx)
            if self._transcription_ids:
                self._saved_settings.transcription_model = self._transcription_ids[idx]

    def _apply_strategy_selection(self, index: int, prompt_key: bool = True) -> None:
        """Apply a Processing Strategy dropdown choice. Real-time switches the
        transcription engine to a streaming one (the default is Deepgram, kept
        if one is already selected); chunk/semantic switch back to a segmented
        engine."""
        sel = apply_strategy(self._saved_settings, index)
        if sel is None:
            return
        log(f"Processing strategy: {sel}", level="INFO")
        self._refresh_transcription_provider_combo()
        self._refresh_transcription_model_combo(reset_default=True)
        self._refresh_source_language_combo()
        self._sync_advanced_enabled_states()
        self._apply_effective_subtitle_mode()
        self._save_current_settings()
        if prompt_key and sel == "realtime":
            key_provider = get_streaming_key_provider(
                self._saved_settings.transcription_provider
            )
            if not has_usable_key(key_provider):
                self._prompt_provider_key(key_provider)

    def _on_transcription_provider_change(self) -> None:
        idx = self.transcription_provider_combo.current()
        if idx is None or not (0 <= idx < len(self._transcription_provider_ids)):
            return
        provider = self._transcription_provider_ids[idx]
        if provider == self._saved_settings.transcription_provider:
            return
        # This dropdown offers same-mode engines only (streaming ones under
        # real-time, segmented ones under chunk/semantic — the Processing
        # Strategy switches between the two), so membership just re-derives
        # the current pipeline mode.
        self._saved_settings.transcription_provider = provider
        self._saved_settings.pipeline_mode = (
            PIPELINE_MODE_STREAMING
            if provider in STREAMING_TRANSCRIPTION_PROVIDERS
            else PIPELINE_MODE_SEGMENTED
        )
        log(f"Transcription provider: {provider}", level="INFO")
        # Keep the user's "Use Default" choice (they unlocked it to get here).
        self._refresh_transcription_model_combo(reset_default=False)
        self._sync_advanced_enabled_states()
        self._save_current_settings()

        # Prompt for the engine's key right away if none is stored yet
        # (openai_realtime authenticates with the OpenAI key).
        key_provider = get_streaming_key_provider(provider)
        if not has_usable_key(key_provider):
            self._prompt_provider_key(key_provider)
        # A streaming socket is opened with one fixed engine — switching it
        # while running reconnects the stream (no-op in segmented mode).
        self._restart_pipeline_for_live_change()

    def _on_hide_subtitle_on_stop_change(self) -> None:
        enabled = self.hide_subtitle_on_stop_var.get()
        self._saved_settings.hide_subtitle_on_stop = enabled
        log(f"Hide subtitle on stop: {'on' if enabled else 'off'}", level="INFO")
        self._save_current_settings()
        if enabled and not self._running:
            self._destroy_subtitle_window()
        elif (
            not enabled
            and not self._running
            and (not self.subtitle_window or not self.subtitle_window.winfo_exists())
        ):
            self._create_subtitle_window()

    def _on_always_on_top_change(self) -> None:
        enabled = self.always_on_top_var.get()
        self._saved_settings.always_on_top = enabled
        # Applies live to both windows: the overlay drops/regains topmost, and
        # the control panel re-evaluates (it stays topmost only while an
        # overlay is open and this setting is on).
        if self.subtitle_window and self.subtitle_window.winfo_exists():
            self.subtitle_window.set_always_on_top(enabled)
        self._apply_control_window_topmost()
        log(f"Always on top: {'on' if enabled else 'off'}", level="INFO")
        self._save_current_settings()

    def _toggle_advanced_settings(self) -> None:
        self.advanced_visible = not self.advanced_visible
        if self.advanced_visible:
            self._advanced_toggle_arrow.configure(text="▴")
            self.advanced_frame.grid(row=1, column=0, sticky="ew", padx=2, pady=(0, 14))
            self.after(80, lambda: self.advanced_frame.focus_set())
        else:
            self._advanced_toggle_arrow.configure(text="▾")
            self.advanced_frame.grid_forget()
        # Expanding the Advanced card makes the right column much taller — switch
        # the columns between bottom-aligned (collapsed) and top-anchored
        # (expanded). See _layout_sidebar_cards.
        self._layout_sidebar_cards()

    def _toggle_log_panel(self) -> None:
        self._log_collapsed = not self._log_collapsed
        self._saved_settings.log_panel_collapsed = self._log_collapsed
        # geometry() reports CTk's logical units; winfo_height() reports raw
        # pixels. Feeding raw pixels back into geometry() re-applies window
        # scaling and grows the window by the DPI factor on every toggle, so
        # read the current logical height back out of geometry() instead.
        import re

        m = re.match(r"(\d+)x(\d+)", self.geometry())
        current_width = int(m.group(1)) if m else self._MIN_W
        current_height = int(m.group(2)) if m else self._MIN_H
        # Keep the user's chosen width across the toggle (clamped to the min):
        # the log panel appears within the current width (the sidebar shrinks to
        # 500px) instead of snapping the window to a fixed per-mode width.
        current_width = max(current_width, self._MIN_W)
        if self._log_collapsed:
            # Collapsed: hide the log, reflow into the 2-column card grid.
            self.content.grid_remove()
            self.grid_columnconfigure(0, weight=1, minsize=self._MIN_W)
            self.grid_columnconfigure(1, weight=0, minsize=0)
            self.minsize(self._MIN_W, self._MIN_H)
            self.geometry(f"{current_width}x{current_height}")
            self._log_toggle_btn.configure(text="▶")
        else:
            # Expanded: single-column sidebar + log panel (classic look).
            self.grid_columnconfigure(0, weight=0, minsize=500)
            self.grid_columnconfigure(1, weight=1)
            self.content.grid()
            self.minsize(self._MIN_W, self._MIN_H)
            self.geometry(f"{current_width}x{current_height}")
            self._log_toggle_btn.configure(text="◀")
        self._layout_sidebar_cards()
        self._save_current_settings()

    def _on_provider_change(self) -> None:
        idx = self.provider_combo.current()
        if idx is None or not (0 <= idx < len(self._provider_ids)):
            return
        provider = self._provider_ids[idx]
        if provider == self._saved_settings.ai_provider:
            return
        self._saved_settings.ai_provider = provider
        log(f"Translation provider: {provider}", level="INFO")

        # Repopulate the translation-model dropdown with the new provider's
        # models (transcription is chosen independently).
        translation_choices = get_model_choices(provider, "translation")
        self._model_display_names = [name for name, _mid in translation_choices]
        self._model_ids = [mid for _name, mid in translation_choices]
        self.model_combo.configure(values=self._model_display_names)

        # Select the new provider's default model as a starting point, keeping
        # the current "Use Default" state (the user unlocked it to get here, so
        # changing provider must not silently re-lock the dropdowns).
        default_model = get_default_model(provider, "translation")
        default_idx = (
            self._model_ids.index(default_model)
            if default_model in self._model_ids
            else 0
        )
        self.model_combo.current(default_idx)
        if self._model_ids:
            self._saved_settings.translation_model = self._model_ids[default_idx]
        self._sync_advanced_enabled_states()
        self._save_current_settings()

        # Ask for the provider's key if none is stored yet
        if not has_usable_key(provider):
            self.on_change_key()

    def _on_model_change(self) -> None:
        idx = self.model_combo.current()
        if idx is not None and 0 <= idx < len(self._model_ids):
            self._saved_settings.translation_model = self._model_ids[idx]
            log(f"Translation model: {self._model_ids[idx]}", level="INFO")
        self._save_current_settings()

    def _on_transcription_model_change(self) -> None:
        idx = self.transcription_combo.current()
        if idx is not None and 0 <= idx < len(self._transcription_ids):
            self._saved_settings.transcription_model = self._transcription_ids[idx]
            log(f"Transcription model: {self._transcription_ids[idx]}", level="INFO")
        self._save_current_settings()
        # Streaming (Deepgram) fixes the model at connect; reconnect to apply.
        self._restart_pipeline_for_live_change()

    def _on_use_default_translation_change(self, save: bool = True) -> None:
        use_default = self.use_default_translation_var.get()
        self._saved_settings.use_default_translation_model = use_default
        if use_default:
            # "Standard" restores the whole section to the recommended setup —
            # the default provider AND its default model, not just the model.
            # The provider dropdown greys out while Standard is on, so it must
            # show the real default, not a stale custom pick (a greyed
            # "Anthropic Claude" next to a ticked "Standard" reads as broken).
            self._saved_settings.ai_provider = DEFAULT_AI_PROVIDER
            self._refresh_translation_provider_combo()
            provider = self._saved_settings.ai_provider  # post key-filter fallback
            translation_choices = get_model_choices(provider, "translation")
            self._model_display_names = [name for name, _mid in translation_choices]
            self._model_ids = [mid for _name, mid in translation_choices]
            self.model_combo.configure(values=self._model_display_names)
            default_model = get_default_model(provider, "translation")
            default_idx = (
                self._model_ids.index(default_model)
                if default_model in self._model_ids
                else 0
            )
            self.model_combo.current(default_idx)
            if self._model_ids:
                self._saved_settings.translation_model = self._model_ids[default_idx]
        self._sync_advanced_enabled_states()
        if save:
            self._save_current_settings()

    def _repair_default_provider(self) -> None:
        """Persist and log a provider-default repair. Runs before any widgets
        exist; the rule itself lives in gui/control_state.py."""
        s = self._saved_settings
        stale = repair_default_provider(s)
        if stale is None:
            return
        save_settings(s)
        log(
            f"Repaired inconsistent provider default: {stale} -> {s.ai_provider} "
            f"(use default: {s.use_default_translation_model})",
            level="INFO",
        )

    def _on_use_default_transcription_change(
        self, save: bool = True, reset_provider: bool = True
    ) -> None:
        use_default = self.use_default_transcription_var.get()
        self._saved_settings.use_default_transcription_model = use_default
        if use_default:
            # "Standard" restores the section to the recommended setup — the
            # default engine for the current strategy AND its default model
            # (real-time → Deepgram; chunk/semantic → the segmented default).
            # Skipped when a strategy/provider switch already set the provider
            # and is only re-pinning the model (reset_provider=False).
            if reset_provider:
                default_provider = (
                    DEFAULT_STREAMING_TRANSCRIPTION_PROVIDER
                    if self._saved_settings.pipeline_mode == PIPELINE_MODE_STREAMING
                    else DEFAULT_SEGMENTED_TRANSCRIPTION_PROVIDER
                )
                self._saved_settings.transcription_provider = default_provider
                self._refresh_transcription_provider_combo()
            provider = self._saved_settings.transcription_provider
            choices = get_model_choices(provider, "transcription")
            self._transcription_display_names = [name for name, _mid in choices]
            self._transcription_ids = [mid for _name, mid in choices]
            self.transcription_combo.configure(values=self._transcription_display_names)
            default_model = get_default_model(provider, "transcription")
            default_idx = (
                self._transcription_ids.index(default_model)
                if default_model in self._transcription_ids
                else 0
            )
            self.transcription_combo.current(default_idx)
            if self._transcription_ids:
                self._saved_settings.transcription_model = self._transcription_ids[
                    default_idx
                ]
        self._sync_advanced_enabled_states()
        if save:
            self._save_current_settings()
            # Pinning/unpinning changes the effective streaming model → apply.
            self._restart_pipeline_for_live_change()

    def _on_strategy_change(self) -> None:
        selection = self.strategy_combo.current()
        if selection is None:
            return
        self._apply_strategy_selection(selection, prompt_key=True)

    def _update_speed_button_states(self) -> None:
        mode = self._effective_subtitle_mode()
        if mode == SUBTITLE_MODE_CONTINUOUS:
            self.mode_controls.grid()
            self.speed_row.grid(row=0, column=0)
            self.transparent_checkbox.grid_forget()
            # Catch-up only applies to the continuous ticker.
            self.adaptive_catchup_cb.grid()
            self.show_interim_cb.grid_remove()
        elif mode == SUBTITLE_MODE_STATIC:
            self.mode_controls.grid()
            self.speed_row.grid_forget()
            self.transparent_checkbox.grid(row=0, column=0, pady=4)
            self.adaptive_catchup_cb.grid_remove()
            self.show_interim_cb.grid_remove()
        else:  # realtime feed: no ticker speed, no transparent-static option
            # Hide the whole controls frame, not just its children: an EMPTY
            # CTkFrame falls back to its default 200x200 size request and
            # blows up the row (the startup-gap bug when Realtime is the
            # saved subtitle mode).
            self.mode_controls.grid_remove()
            self.speed_row.grid_forget()
            self.transparent_checkbox.grid_forget()
            self.adaptive_catchup_cb.grid_remove()
            # Live line is a Realtime-mode concept — offer its toggle here only.
            self.show_interim_cb.grid()

    def _save_current_settings(self) -> None:
        try:
            self._saved_settings.gui_language = self.gui_lang_code
            self._saved_settings.theme_mode = self._theme_mode
            save_settings(self._saved_settings)
        except Exception as exc:
            log(f"Failed to save settings: {exc}", level="ERROR")

    def _on_gui_language_change(self) -> None:
        selection = self.gui_lang_combo.current()
        if selection is None or not (0 <= selection < len(GUI_LANGUAGE_CODES)):
            return
        self.gui_lang_code = GUI_LANGUAGE_CODES[selection]
        self._gui_lang = self.gui_lang_code
        self.gui_texts = load_gui_translations(self.gui_lang_code)
        self._t = self.gui_texts
        self._update_all_ui_texts()
        self._save_current_settings()
        log(
            self.gui_texts.get(
                "log_gui_language_changed", "GUI language changed to: {language}"
            ).format(language=self.gui_lang_combo.get()),
            level="INFO",
        )

    def _on_theme_change(self, selected: str) -> None:
        light_label = self.gui_texts.get("theme_light", "Light")
        theme_mode = "light" if selected == light_label else "dark"
        self._apply_theme(theme_mode)
        self._save_current_settings()

    def _on_subtitle_theme_change(self, selected: str) -> None:
        light_label = self.gui_texts.get("theme_light", "Light")
        subtitle_theme_mode = "light" if selected == light_label else "dark"
        self._saved_settings.subtitle_theme_mode = subtitle_theme_mode
        self._apply_subtitle_theme(subtitle_theme_mode)
        self._save_current_settings()

    def _apply_subtitle_theme(self, subtitle_theme_mode: str) -> None:
        """Apply theme to the subtitle overlay window only (independent of control panel)."""
        self._saved_settings.subtitle_theme_mode = subtitle_theme_mode
        if self.subtitle_window and self.subtitle_window.winfo_exists():
            self.subtitle_window.set_theme(subtitle_theme_mode)

    def _apply_theme(self, theme_mode: str) -> None:
        self._theme_mode = theme_mode
        self._colors = self._palette(theme_mode)

        self.configure(fg_color=self._colors["app_bg"])
        self.sidebar_container.configure(fg_color=self._colors["sidebar"])
        self._sidebar_header.configure(fg_color=self._colors["sidebar"])
        self._update_banner.configure(fg_color=self._colors["accent_soft"])
        self._update_banner_label.configure(text_color=self._colors["accent"])
        self._update_banner_close.configure(text_color=self._colors["accent"])
        self.sidebar.configure(fg_color=self._colors["sidebar"])
        self.content.configure(fg_color=self._colors["app_bg"])
        self._restore_control_window_surface()
        # The OS titlebar is set once at startup and doesn't follow a runtime
        # switch — repaint it (main window here, settings window below).
        apply_dark_titlebar(self, dark=theme_mode == "dark")

        for frame in self._shadow_frames:
            frame.configure(fg_color=self._colors["shadow"])
        for card in self._cards:
            card.configure(
                fg_color=self._colors["card"], border_color=self._colors["border"]
            )
        for panel in self._main_panels:
            panel.configure(
                fg_color=self._colors["panel_soft"], border_color=self._colors["border"]
            )
        self.log_panel.configure(
            fg_color=self._colors["panel"], border_color=self._colors["border"]
        )
        self.log_text.configure(
            fg_color=self._colors["log_bg"],
            text_color=self._colors["log_text"],
            border_color=self._colors["border"],
        )
        for label in self._labels + self._section_titles:
            label.configure(text_color=self._colors["text"])
        for label in self._muted_labels:
            label.configure(text_color=self._colors["muted"])
        for symbol in self._symbol_labels:
            symbol.configure(
                text_color=self._colors["accent"], fg_color=self._colors["panel_soft"]
            )
        for button in self._buttons:
            button.configure(
                fg_color=self._colors["button"],
                hover_color=self._colors["button_hover"],
                text_color=self._colors["text"],
            )
        for combo in self._combos:
            combo.configure(
                fg_color=self._colors["entry"],
                border_color=self._colors["entry_border"],
                button_color=self._colors["entry"],
                button_hover_color=self._colors["panel_soft"],
                text_color=self._colors["text"],
                dropdown_fg_color=self._colors["panel"],
                dropdown_hover_color=self._colors["button_hover"],
                dropdown_text_color=self._colors["text"],
            )
        for cb in self._checkboxes:
            cb.configure(
                fg_color=self._colors["accent"],
                hover_color=self._colors["accent_hover"],
                border_color=self._colors["entry_border"],
                text_color=self._colors["text"],
            )
        if hasattr(self, "input_level_bar"):
            self.input_level_bar.set_palette(
                track_color=self._colors["panel_soft"],
                border_color=self._colors["border"],
                green_color=self._INPUT_LEVEL_GREEN,
                warning_color=self._INPUT_LEVEL_WARNING,
                danger_color=self._INPUT_LEVEL_DANGER,
            )
            self.input_level_test_btn.configure(
                fg_color=self._colors["button"],
                hover_color=self._colors["button_hover"],
                text_color=self._colors["text"],
            )
            self._input_level_ui_state = None  # re-sync button state next poll
        # The history/batch windows are rebuilt from scratch on open; close a
        # stale one so it isn't left with old-theme/old-language widgets.
        self._close_history_window()
        self._close_batch_window()
        self._close_announce_window()
        # Update settings window if it's open
        if self._settings_win_exists():
            # Settings stays open across the switch (it hosts the toggle), so its
            # caption won't repaint without a forced hide/show — unlike the main
            # window, which its surface-restore already nudges.
            apply_dark_titlebar(
                self._settings_win, dark=theme_mode == "dark", force_repaint=True
            )
            self._settings_win.configure(fg_color=self._colors["app_bg"])
            self._settings_scroll.configure(fg_color=self._colors["sidebar"])
            self._settings_bottom_bar.configure(fg_color=self._colors["sidebar"])
            for label in getattr(self, "_settings_labels", []):
                try:
                    label.configure(text_color=self._colors["text"])
                except Exception:
                    pass
            for label in getattr(self, "_settings_muted_labels", []):
                try:
                    label.configure(text_color=self._colors["muted"])
                except Exception:
                    pass
            for btn in getattr(self, "_settings_buttons", []):
                try:
                    btn.configure(
                        fg_color=self._colors["button"],
                        hover_color=self._colors["button_hover"],
                        text_color=self._colors["text"],
                    )
                except Exception:
                    pass
            for combo in getattr(self, "_settings_combos", []):
                try:
                    combo.configure(
                        fg_color=self._colors["entry"],
                        border_color=self._colors["entry_border"],
                        button_color=self._colors["entry"],
                        button_hover_color=self._colors["panel_soft"],
                        text_color=self._colors["text"],
                        dropdown_fg_color=self._colors["panel"],
                        dropdown_hover_color=self._colors["button_hover"],
                        dropdown_text_color=self._colors["text"],
                    )
                except Exception:
                    pass
            for cb in getattr(self, "_settings_checkboxes", []):
                try:
                    cb.configure(
                        fg_color=self._colors["accent"],
                        hover_color=self._colors["accent_hover"],
                        border_color=self._colors["entry_border"],
                        text_color=self._colors["text"],
                    )
                except Exception:
                    pass
            for sw in getattr(self, "_settings_switches", []):
                try:
                    sw.configure(
                        progress_color=self._colors["accent"],
                        fg_color=self._colors["entry_border"],
                        text_color=self._colors["text"],
                    )
                except Exception:
                    pass
            self.theme_segment.configure(
                fg_color=self._colors["button"],
                selected_color=self._colors["accent"],
                selected_hover_color=self._colors["accent_hover"],
                unselected_color=self._colors["button"],
                unselected_hover_color=self._colors["button_hover"],
                text_color=self._colors["text"],
            )
            for card in getattr(self, "_settings_cards", []):
                try:
                    card.configure(
                        fg_color=self._colors["card"],
                        border_color=self._colors["border"],
                    )
                except Exception:
                    pass
            for sym in getattr(self, "_settings_symbol_labels", []):
                try:
                    sym.configure(
                        text_color=self._colors["accent"],
                        fg_color=self._colors["panel_soft"],
                    )
                except Exception:
                    pass
        self.height_slider.configure(
            button_color=self._colors["accent"],
            progress_color=self._colors["accent"],
            fg_color=self._colors["button"],
            button_hover_color=self._colors["accent_hover"],
        )
        self.height_value_label.configure(text_color=self._colors["text"])
        self.speed_label.configure(text_color=self._colors["text"])
        # Control panel theme does NOT touch subtitle window — see _apply_subtitle_theme()
        if self._settings_win_exists():
            try:
                self.subtitle_theme_segment.configure(
                    fg_color=self._colors["button"],
                    selected_color=self._colors["accent"],
                    selected_hover_color=self._colors["accent_hover"],
                    unselected_color=self._colors["button"],
                    unselected_hover_color=self._colors["button_hover"],
                    text_color=self._colors["text"],
                )
            except Exception:
                pass
        self._set_status(self._running)
        # Re-apply disabled states: after colour update, the new border_color must
        # be used as the "greyed out" text colour for disabled combos.
        self._sync_advanced_enabled_states()
        self.after(50, self._restore_control_window_surface)

    def _update_all_ui_texts(self) -> None:
        # The history/batch windows are rebuilt from scratch on open; close a
        # stale one so it isn't left with old-theme/old-language widgets.
        self._close_history_window()
        self._close_batch_window()
        self._close_announce_window()
        if hasattr(self, "input_level_test_btn"):
            self._input_level_ui_state = None  # re-sync button text next poll
        for label in self._labels + self._section_titles:
            key = getattr(label, "_text_key", None)
            if key:
                symbol = getattr(label, "_symbol", None)
                text = self.gui_texts.get(key, key)
                label.configure(text=f"{symbol}  {text}" if symbol else text)
        for label in self._muted_labels:
            key = getattr(label, "_text_key", None)
            if key:
                label.configure(text=self.gui_texts.get(key, key))
        for button in self._buttons:
            key = getattr(button, "_text_key", None)
            if key:
                symbol = getattr(button, "_symbol", None)
                text = self.gui_texts.get(key, key)
                button.configure(text=f"{symbol}  {text}" if symbol else text)
        for cb in self._checkboxes:
            key = getattr(cb, "_text_key", None)
            if key:
                cb.configure(text=self.gui_texts.get(key, key))
        # The "Use Default" checkbox width changed with the language, so the
        # provider dropdowns must be re-padded to stay aligned with it.
        self._align_provider_combo_widths()

        self.strategy_running_hint.configure(
            text=self.gui_texts.get("hint_stop_to_change", "⚠ Stop program to change")
        )
        if self._update_available is not None:
            self._update_banner_label.configure(text=self._update_banner_text())
        self.start_btn.configure(text=f"▶  {self._clean_action_label('start')}")
        self.stop_btn.configure(text=f"■  {self._clean_action_label('stop')}")
        self.logs_label.configure(text=f"▤  {self.gui_texts.get('logs', 'Logs')}")

        self._refresh_subtitle_mode_combo()

        # Update settings window widgets if the window is open
        if self._settings_win_exists():
            self.theme_segment.configure(
                values=[
                    self.gui_texts.get("theme_dark", "Dark"),
                    self.gui_texts.get("theme_light", "Light"),
                ]
            )
            self.theme_segment.set(
                self.gui_texts.get(
                    "theme_light" if self._theme_mode == "light" else "theme_dark",
                    "Dark",
                )
            )
            try:
                _sub_mode = getattr(
                    self._saved_settings, "subtitle_theme_mode", self._theme_mode
                )
                self.subtitle_theme_segment.configure(
                    values=[
                        self.gui_texts.get("theme_dark", "Dark"),
                        self.gui_texts.get("theme_light", "Light"),
                    ]
                )
                self.subtitle_theme_segment.set(
                    self.gui_texts.get(
                        "theme_light" if _sub_mode == "light" else "theme_dark", "Dark"
                    )
                )
            except Exception:
                pass
            for label in getattr(self, "_settings_muted_labels", []):
                key = getattr(label, "_text_key", None)
                if key:
                    label.configure(text=self.gui_texts.get(key, key))
            for label in getattr(self, "_settings_labels", []):
                key = getattr(label, "_text_key", None)
                if key:
                    symbol = getattr(label, "_symbol", None)
                    text = self.gui_texts.get(key, key)
                    label.configure(text=f"{symbol}  {text}" if symbol else text)
            for button in getattr(self, "_settings_buttons", []):
                key = getattr(button, "_text_key", None)
                if key:
                    symbol = getattr(button, "_symbol", None)
                    text = self.gui_texts.get(key, key)
                    button.configure(text=f"{symbol}  {text}" if symbol else text)
            for cb in getattr(self, "_settings_checkboxes", []):
                key = getattr(cb, "_text_key", None)
                if key:
                    cb.configure(text=self.gui_texts.get(key, key))
            for sw in getattr(self, "_settings_switches", []):
                key = getattr(sw, "_text_key", None)
                if key:
                    sw.configure(text=self.gui_texts.get(key, key))

        self._strategy_display_names = self._strategy_labels()
        current_strategy = self.strategy_combo.current()
        self.strategy_combo.configure(values=self._strategy_display_names)
        if current_strategy is not None and 0 <= current_strategy < len(
            self._strategy_display_names
        ):
            self.strategy_combo.current(current_strategy)
        self.strategy_running_hint.configure(
            text=self.gui_texts.get("hint_stop_to_change", "⚠ Stop program to change")
        )

        if self.advanced_visible:
            self._advanced_toggle_arrow.configure(text="▴")
        else:
            self._advanced_toggle_arrow.configure(text="▾")
        self._set_status(self._running)
        self._update_speed_button_states()

    def on_close(self) -> None:
        # Silence callback-exception reporting for the rest of this deliberate
        # shutdown. Tearing the window down (and cancelling pending callbacks
        # below) makes CustomTkinter delete already-gone Tcl commands — notably
        # in CTkTextbox.destroy — which Tk reports as benign "can't delete Tcl
        # command" tracebacks. None of it affects the exit; just keep it quiet.
        try:
            self.report_callback_exception = lambda *a: None
        except Exception:
            pass
        try:
            self._saved_settings.window_geometry = self.geometry()
            self._save_current_settings()
        except Exception:
            pass
        try:
            self.controller.stop()
        except Exception:
            pass
        # Stop the focus/map handlers from scheduling new surface-restore jobs
        # while the window is being torn down.
        for seq in ("<FocusIn>", "<Map>"):
            try:
                self.unbind(seq)
            except Exception:
                pass
        # Cancel EVERY pending after() callback, not just the tracked poll jobs.
        # An untracked scheduled callback (e.g. a startup lambda) that fires
        # mid-destroy raises Tcl's 'invalid command name ...<lambda>' and can
        # leave mainloop() spinning after the window has closed — the reported
        # freeze on exit that needed a Ctrl+C.
        try:
            for after_id in self.tk.call("after", "info"):
                try:
                    self.after_cancel(after_id)
                except Exception:
                    pass
        except Exception:
            pass
        if self.subtitle_window:
            try:
                self.subtitle_window.destroy()
            except Exception:
                pass
        # quit() guarantees mainloop() returns even if a teardown callback
        # misbehaves; destroy() then frees the widgets.
        self.quit()
        self.destroy()
