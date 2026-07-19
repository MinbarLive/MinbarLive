"""Announcement (megaphone) window + overlay lifecycle (mixin for AppGUI).

A small operator window (reached from the header megaphone button) to type a
custom message, pick how long it shows (10s / 30s / 1m / 5m / until stopped)
and send it. The message renders big and centred on the subtitle overlay,
above the subtitles but below the disclaimer pill (see
SubtitleWindow.set_announcement).

The active-announcement STATE (text, timer) lives on the AppGUI instance, not
in the window or the SubtitleWindow — an "until stopped" announcement must
survive a translation stop AND the subtitle window being destroyed/recreated
(user decision). The window is rebuilt from scratch on open; theme/GUI-language
changes close it (see AppGUI._apply_theme / _update_all_ui_texts). Uses AppGUI
services: ``_colors``, ``gui_texts``, ``_combo`` / ``_set_toplevel_icon``,
``_saved_settings``, ``_save_current_settings``, ``_create_subtitle_window`` /
``_destroy_subtitle_window`` and ``subtitle_window``.
"""

import tkinter as tk

import customtkinter as ctk

from config import (
    ANNOUNCEMENT_DURATIONS_SECONDS,
    ANNOUNCEMENT_FAVORITES_MAX,
    ANNOUNCEMENT_HISTORY_MAX,
)
from gui.scaling import centered_position
from utils.logging import log

# GUI keys for the preset durations, parallel to ANNOUNCEMENT_DURATIONS_SECONDS
# (a 0-second entry means "until stopped").
_DURATION_KEYS = [
    "announce_duration_10s",
    "announce_duration_30s",
    "announce_duration_1m",
    "announce_duration_5m",
    "announce_duration_until_stopped",
]
_DEFAULT_DURATION_INDEX = 1  # 30 seconds


class AnnounceViewMixin:
    """Announcement window + overlay lifecycle, hosted by AppGUI."""

    def _init_announce_state(self) -> None:
        self._announce_win: ctk.CTkToplevel | None = None
        # The message currently shown on the overlay ("" = none). Owned here so
        # it survives a translation stop and subtitle-window recreation.
        self._announcement_text_active: str = ""
        self._announcement_until_stopped: bool = False
        self._announcement_job: str | None = None
        # Remembered duration choice (index into ANNOUNCEMENT_DURATIONS_SECONDS);
        # persisted across restarts (utils/settings), defaults to 30s.
        idx = self._saved_settings.announcement_duration_index
        if not (0 <= idx < len(ANNOUNCEMENT_DURATIONS_SECONDS)):
            idx = _DEFAULT_DURATION_INDEX
        self._announce_duration_index: int = idx

    # ── window lifecycle ────────────────────────────────────────────────────

    def _announce_win_exists(self) -> bool:
        return (
            getattr(self, "_announce_win", None) is not None
            and self._announce_win.winfo_exists()
        )

    def _close_announce_window(self) -> None:
        if self._announce_win_exists():
            self._announce_win.destroy()
        self._announce_win = None

    def _open_announce_window(self) -> None:
        if self._announce_win_exists():
            self._announce_win.lift()
            self._announce_win.focus()
            return

        win = ctk.CTkToplevel(self)
        win.title(self.gui_texts.get("announce_title", "Announcement"))
        win.resizable(False, False)
        win.configure(fg_color=self._colors["app_bg"])
        # Transparent while building + sizing to hide the flash, then fade in
        # (alpha, not withdraw — the transient window must never be unmapped).
        try:
            win.attributes("-alpha", 0.0)
        except tk.TclError:
            pass
        win.after(200, lambda: self._set_toplevel_icon(win))
        win.transient(self)
        self._announce_win = win
        self._build_announce_widgets(win)
        self._resize_announce_window()
        try:
            win.attributes("-alpha", 1.0)
        except tk.TclError:
            pass
        # If an overlay is open the control panel is -topmost; match it so this
        # window isn't hidden behind the panel.
        self._raise_announce_window()

    def _resize_announce_window(self) -> None:
        """Size the window to its content's natural height (varies with GUI
        language and how many recent items are listed) and centre it.

        winfo_reqheight() is physical px while CTkToplevel.geometry() scales
        WxH by the window's DPI factor — convert to logical units (see
        _resize_batch_window)."""
        if not self._announce_win_exists():
            return
        win = self._announce_win
        win.update_idletasks()
        w = 460
        scaling = ctk.ScalingTracker.get_window_scaling(win)
        h = int(win.winfo_reqheight() / scaling) + 1
        x, y = centered_position(self, w, h)
        win.geometry(f"{w}x{h}+{x}+{y}")

    # ── widgets ─────────────────────────────────────────────────────────────

    def _build_announce_widgets(self, parent: ctk.CTkBaseClass) -> None:
        card = ctk.CTkFrame(
            parent,
            fg_color=self._colors["card"],
            border_color=self._colors["border"],
            border_width=2,
            corner_radius=24,
        )
        card.pack(fill="both", expand=True, padx=16, pady=16)
        card.grid_columnconfigure(0, weight=1, uniform="announce_actions")
        card.grid_columnconfigure(1, weight=1, uniform="announce_actions")

        header = ctk.CTkLabel(
            card,
            text=self.gui_texts.get("announce_title", "Announcement"),
            image=self._dashboard.icon_image("announcement", 19),
            compound="left",
            font=ctk.CTkFont(family="Segoe UI", size=18, weight="bold"),
            text_color=self._colors["text"],
            anchor="w",
        )
        header.grid(row=0, column=0, columnspan=2, sticky="ew", padx=20, pady=(18, 0))

        sub = ctk.CTkLabel(
            card,
            text=self.gui_texts.get(
                "announce_sub", "Show a message big on the subtitle screen"
            ),
            font=ctk.CTkFont(family="Segoe UI", size=13),
            text_color=self._colors["muted"],
            anchor="w",
        )
        sub.grid(row=1, column=0, columnspan=2, sticky="ew", padx=20, pady=(0, 12))

        self._announce_textbox = ctk.CTkTextbox(
            card,
            height=110,
            corner_radius=16,
            border_width=1,
            border_color=self._colors["entry_border"],
            fg_color=self._colors["entry"],
            text_color=self._colors["text"],
            font=ctk.CTkFont(family="Segoe UI", size=15),
            wrap="word",
        )
        self._announce_textbox.grid(
            row=2, column=0, columnspan=2, sticky="ew", padx=20, pady=(0, 14)
        )

        duration_label = ctk.CTkLabel(
            card,
            text=self.gui_texts.get("announce_duration_label", "Show for"),
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            text_color=self._colors["text"],
            anchor="w",
        )
        duration_label.grid(row=3, column=0, columnspan=2, sticky="ew", padx=20)

        duration_values = [
            self.gui_texts.get(key, key) for key in _DURATION_KEYS
        ]
        self._announce_duration_combo = self._combo(
            card, duration_values, register=False
        )
        idx = self._announce_duration_index
        if not (0 <= idx < len(duration_values)):
            idx = _DEFAULT_DURATION_INDEX
        self._announce_duration_combo.set(duration_values[idx])
        self._announce_duration_combo.grid(
            row=4, column=0, columnspan=2, sticky="ew", padx=20, pady=(6, 16)
        )

        self._announce_favorites_label = ctk.CTkLabel(
            card,
            text=self.gui_texts.get("announce_favorites", "Favorites"),
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            text_color=self._colors["text"],
            anchor="w",
        )
        self._announce_favorites_label.grid(
            row=5, column=0, columnspan=2, sticky="ew", padx=20
        )

        self._announce_favorites_frame = ctk.CTkFrame(card, fg_color="transparent")
        self._announce_favorites_frame.grid(
            row=6, column=0, columnspan=2, sticky="ew", padx=20, pady=(6, 14)
        )
        self._announce_favorites_frame.grid_columnconfigure(0, weight=1)
        self._populate_announce_favorites()

        recent_label = ctk.CTkLabel(
            card,
            text=self.gui_texts.get("announce_recent", "Recent"),
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            text_color=self._colors["text"],
            anchor="w",
        )
        recent_label.grid(row=7, column=0, columnspan=2, sticky="ew", padx=20)

        self._announce_recent_frame = ctk.CTkFrame(card, fg_color="transparent")
        self._announce_recent_frame.grid(
            row=8, column=0, columnspan=2, sticky="ew", padx=20, pady=(6, 14)
        )
        self._announce_recent_frame.grid_columnconfigure(0, weight=1)
        self._populate_announce_recent()

        self._announce_send_btn = ctk.CTkButton(
            card,
            text=self.gui_texts.get("announce_send", "Send"),
            command=self._send_announcement,
            height=48,
            corner_radius=16,
            font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
            fg_color=self._colors["accent"],
            hover_color=self._colors["accent_hover"],
            text_color="#ffffff",
        )
        self._announce_send_btn.grid(
            row=9, column=0, sticky="ew", padx=(20, 8), pady=(0, 20)
        )

        self._announce_stop_btn = ctk.CTkButton(
            card,
            text=self.gui_texts.get("announce_stop", "Stop"),
            command=self._stop_announcement,
            height=48,
            corner_radius=16,
            font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
            fg_color=self._colors["button"],
            hover_color=self._colors["button_hover"],
            text_color=self._colors["text"],
        )
        self._announce_stop_btn.grid(
            row=9, column=1, sticky="ew", padx=(8, 20), pady=(0, 20)
        )
        self._refresh_announce_stop_state()

    def _announce_preview(self, text: str) -> str:
        preview = " ".join(text.split())
        if len(preview) > 42:
            preview = preview[:41] + "…"
        return preview

    def _populate_announce_favorites(self) -> None:
        """(Re)build the starred list — hidden entirely (label + frame) while
        empty, so users who never favorite anything see no extra clutter."""
        label = getattr(self, "_announce_favorites_label", None)
        frame = getattr(self, "_announce_favorites_frame", None)
        if frame is None or not frame.winfo_exists():
            return
        for child in frame.winfo_children():
            child.destroy()

        favorites = self._saved_settings.announcement_favorites
        if not favorites:
            if label is not None and label.winfo_exists():
                label.grid_remove()
            frame.grid_remove()
            return
        if label is not None and label.winfo_exists():
            label.grid()
        frame.grid()

        for i, text in enumerate(favorites):
            row_frame = ctk.CTkFrame(frame, fg_color="transparent")
            row_frame.grid(row=i, column=0, sticky="ew", pady=2)
            row_frame.grid_columnconfigure(0, weight=1)

            text_btn = ctk.CTkButton(
                row_frame,
                text=self._announce_preview(text),
                command=lambda t=text: self._load_recent_announcement(t),
                height=36,
                corner_radius=12,
                anchor="w",
                font=ctk.CTkFont(family="Segoe UI", size=13),
                fg_color=self._colors["button"],
                hover_color=self._colors["button_hover"],
                text_color=self._colors["text"],
            )
            text_btn.grid(row=0, column=0, sticky="ew")

            # Filled star, always visible — click to unfavorite.
            star_btn = ctk.CTkButton(
                row_frame,
                text="",
                image=self._dashboard.icon_image("favorite_filled", 16),
                width=36,
                height=36,
                corner_radius=12,
                command=lambda t=text: self._unfavorite_announcement(t),
                fg_color=self._colors["button"],
                hover_color=self._colors["button_hover"],
                text_color=self._colors["accent"],
            )
            star_btn.grid(row=0, column=1, padx=(6, 0))

    def _populate_announce_recent(self) -> None:
        """(Re)build the recent-texts list — one clickable row per remembered
        message, or a muted empty-state label. Favorited texts live only in
        the Favorites section above, never duplicated here."""
        frame = getattr(self, "_announce_recent_frame", None)
        if frame is None or not frame.winfo_exists():
            return
        for child in frame.winfo_children():
            child.destroy()

        favorites = set(self._saved_settings.announcement_favorites)
        history = [
            t for t in self._saved_settings.announcement_history if t not in favorites
        ]
        if not history:
            empty = ctk.CTkLabel(
                frame,
                text=self.gui_texts.get(
                    "announce_recent_empty", "No recent announcements"
                ),
                font=ctk.CTkFont(family="Segoe UI", size=12),
                text_color=self._colors["muted"],
                anchor="w",
            )
            empty.grid(row=0, column=0, sticky="ew", pady=2)
            return

        for i, text in enumerate(history):
            row_frame = ctk.CTkFrame(frame, fg_color="transparent")
            row_frame.grid(row=i, column=0, sticky="ew", pady=2)
            row_frame.grid_columnconfigure(0, weight=1)

            text_btn = ctk.CTkButton(
                row_frame,
                text=self._announce_preview(text),
                command=lambda t=text: self._load_recent_announcement(t),
                height=36,
                corner_radius=12,
                anchor="w",
                font=ctk.CTkFont(family="Segoe UI", size=13),
                fg_color=self._colors["button"],
                hover_color=self._colors["button_hover"],
                text_color=self._colors["text"],
            )
            text_btn.grid(row=0, column=0, sticky="ew")

            # Star + delete — both revealed only while the row is hovered.
            star_btn = ctk.CTkButton(
                row_frame,
                text="",
                image=self._dashboard.icon_image("favorite", 16),
                width=36,
                height=36,
                corner_radius=12,
                command=lambda t=text: self._favorite_announcement(t),
                fg_color=self._colors["button"],
                hover_color=self._colors["button_hover"],
                text_color=self._colors["muted"],
            )
            del_btn = ctk.CTkButton(
                row_frame,
                text="",
                image=self._dashboard.icon_image("close", 15),
                width=36,
                height=36,
                corner_radius=12,
                command=lambda t=text: self._delete_recent_announcement(t),
                fg_color=self._colors["button"],
                hover_color=self._colors["button_hover"],
                text_color=self._colors["muted"],
            )
            self._bind_recent_hover(row_frame, [star_btn, del_btn])

    def _bind_recent_hover(
        self, row_frame: ctk.CTkFrame, buttons: list[ctk.CTkButton]
    ) -> None:
        """Show ``buttons`` (left to right, starting at column 1) while the
        pointer is anywhere over ``row_frame`` (the text button, the revealed
        buttons or the gaps). ``<Leave>`` fires when the pointer crosses onto a
        child too, so hiding is gated on the pointer actually having left the
        row's widget subtree."""

        def _show(_e: object = None) -> None:
            if row_frame.winfo_exists():
                for i, btn in enumerate(buttons):
                    btn.grid(row=0, column=i + 1, padx=(6, 0))

        def _maybe_hide() -> None:
            if not row_frame.winfo_exists():
                return
            x, y = row_frame.winfo_pointerxy()
            node = row_frame.winfo_containing(x, y)
            while node is not None:
                if node is row_frame:
                    return  # still inside the row
                node = getattr(node, "master", None)
            for btn in buttons:
                btn.grid_forget()

        def _bind_tree(widget: object) -> None:
            widget.bind("<Enter>", _show, add="+")
            widget.bind("<Leave>", lambda _e: row_frame.after(40, _maybe_hide), add="+")
            for child in widget.winfo_children():
                _bind_tree(child)

        _bind_tree(row_frame)

    def _delete_recent_announcement(self, text: str) -> None:
        self._saved_settings.announcement_history = [
            t for t in self._saved_settings.announcement_history if t != text
        ]
        self._save_current_settings()
        self._populate_announce_recent()
        self._resize_announce_window()

    def _favorite_announcement(self, text: str) -> None:
        """Star a recent text: pin it (immune to history rotation/eviction)
        and remove the now-redundant copy from Recent.

        Refuses once the cap is reached instead of silently evicting the
        oldest favorite — silently dropping a pinned reminder to make room
        for a new one would defeat the point of pinning it (user decision)."""
        existing = self._saved_settings.announcement_favorites
        if text not in existing and len(existing) >= ANNOUNCEMENT_FAVORITES_MAX:
            self._alert(
                self.gui_texts.get("announce_favorites", "Favorites"),
                self.gui_texts.get(
                    "announce_favorites_full",
                    "You can pin up to {max} favorites. Remove one before "
                    "adding another.",
                ).format(max=ANNOUNCEMENT_FAVORITES_MAX),
                parent=self._announce_win if self._announce_win_exists() else None,
            )
            return
        favorites = [t for t in existing if t != text]
        favorites.insert(0, text)
        self._saved_settings.announcement_favorites = favorites[
            :ANNOUNCEMENT_FAVORITES_MAX
        ]
        self._saved_settings.announcement_history = [
            t for t in self._saved_settings.announcement_history if t != text
        ]
        self._save_current_settings()
        self._populate_announce_favorites()
        self._populate_announce_recent()
        self._resize_announce_window()

    def _unfavorite_announcement(self, text: str) -> None:
        """Unstar a text. It is not restored to Recent — sending it again
        will naturally re-add it there."""
        self._saved_settings.announcement_favorites = [
            t for t in self._saved_settings.announcement_favorites if t != text
        ]
        self._save_current_settings()
        self._populate_announce_favorites()
        self._populate_announce_recent()
        self._resize_announce_window()

    def _load_recent_announcement(self, text: str) -> None:
        if not self._announce_win_exists():
            return
        self._announce_textbox.delete("1.0", "end")
        self._announce_textbox.insert("1.0", text)

    def _refresh_announce_stop_state(self) -> None:
        btn = getattr(self, "_announce_stop_btn", None)
        if btn is None or not btn.winfo_exists():
            return
        btn.configure(
            state="normal" if self._has_active_announcement() else "disabled"
        )

    # ── overlay lifecycle ───────────────────────────────────────────────────

    def _has_active_announcement(self) -> bool:
        return bool(self._announcement_text_active)

    def _send_announcement(self) -> None:
        if not self._announce_win_exists():
            return
        text = self._announce_textbox.get("1.0", "end").strip()
        if not text:
            return
        # "Kein Bildschirm" is an authoritative output choice.  Abort before
        # touching history, active state or timers so enabling a screen later
        # cannot resurrect a message that was never shown.
        if not self._subtitle_output_is_enabled():
            self._alert(
                self.gui_texts.get(
                    "announce_no_screen_title", "Kein Bildschirm ausgewählt"
                ),
                self.gui_texts.get(
                    "announce_no_screen_message",
                    "Wähle im Ausgabefenster einen Bildschirm aus, um die "
                    "Durchsage anzuzeigen.",
                ),
                parent=self._announce_win,
            )
            return

        idx = self._announce_duration_combo.current()
        if idx is None or not (0 <= idx < len(ANNOUNCEMENT_DURATIONS_SECONDS)):
            idx = _DEFAULT_DURATION_INDEX
        self._announce_duration_index = idx
        self._saved_settings.announcement_duration_index = idx
        seconds = ANNOUNCEMENT_DURATIONS_SECONDS[idx]

        self._push_announcement_history(text)
        self._announcement_text_active = text
        self._announcement_until_stopped = seconds <= 0

        # A new send replaces any current announcement (its timer is cancelled).
        self._cancel_announcement_timer()
        self._ensure_subtitle_window_for_announcement()
        if self.subtitle_window and self.subtitle_window.winfo_exists():
            self.subtitle_window.set_announcement(text)
        if seconds > 0:
            self._announcement_job = self.after(
                seconds * 1000, self._on_announcement_timeout
            )
        log("Announcement shown on the subtitle overlay.", level="INFO")

        self._populate_announce_recent()
        self._resize_announce_window()
        self._refresh_announce_stop_state()
        # Creating/recreating the subtitle overlay (hide-on-stop) re-topmosts
        # the control panel and can bury this window behind it — force it back
        # to the front.
        self._raise_announce_window()

    def _raise_announce_window(self) -> None:
        """Keep the announcement window above the control panel. Sending a
        message (with hide-on-stop) creates the subtitle overlay, which makes
        the control panel -topmost when always-on-top is on — a plain lift()
        can't rise above a -topmost window, so match its topmost state, then
        lift and focus."""
        if not self._announce_win_exists():
            return
        self._sync_announce_topmost()
        self._announce_win.lift()
        self._announce_win.focus_force()

    def _sync_announce_topmost(self) -> None:
        """Match the announcement window's -topmost to the control panel's, so
        it is neither hidden behind a topmost panel nor left stuck above other
        apps once the overlay (and the panel's topmost) is gone. No focus
        change — safe to call from a timer-driven stop."""
        if not self._announce_win_exists():
            return
        try:
            self._announce_win.attributes(
                "-topmost", self._control_window_should_be_topmost()
            )
        except tk.TclError:
            pass

    def _stop_announcement(self) -> None:
        """Clear the current announcement (Stop button or timer expiry)."""
        self._cancel_announcement_timer()
        was_active = self._has_active_announcement()
        self._announcement_text_active = ""
        self._announcement_until_stopped = False
        if self.subtitle_window and self.subtitle_window.winfo_exists():
            self.subtitle_window.clear_announcement()
        # If the overlay was kept open only to show the announcement while the
        # pipeline is stopped (hide-on-stop), close it again now.
        if (
            was_active
            and not self._running
            and self._saved_settings.hide_subtitle_on_stop
            and self.subtitle_window
            and self.subtitle_window.winfo_exists()
        ):
            self._destroy_subtitle_window()
        self._refresh_announce_stop_state()
        # Destroying the overlay drops the control panel's topmost; keep the
        # (possibly still open) announcement window in sync so it isn't left
        # floating above every other app.
        self._sync_announce_topmost()

    def _on_announcement_timeout(self) -> None:
        self._announcement_job = None
        self._stop_announcement()

    def _cancel_announcement_timer(self) -> None:
        if self._announcement_job is not None:
            try:
                self.after_cancel(self._announcement_job)
            except Exception:
                pass
            self._announcement_job = None

    def _ensure_subtitle_window_for_announcement(self) -> None:
        """The announcement needs a surface. With hide-on-stop and the pipeline
        stopped there is no overlay — create one so the message can show
        (torn down again by _stop_announcement)."""
        if not self._subtitle_output_is_enabled():
            return
        if not (self.subtitle_window and self.subtitle_window.winfo_exists()):
            self._create_subtitle_window()

    def _apply_active_announcement(self) -> None:
        """Re-assert the active announcement onto the current subtitle window.
        Called after (re)creating the overlay so an 'until stopped' message
        reappears across a stop/start or a monitor/height rebuild."""
        if (
            self._announcement_text_active
            and self.subtitle_window
            and self.subtitle_window.winfo_exists()
        ):
            self.subtitle_window.set_announcement(self._announcement_text_active)

    def _push_announcement_history(self, text: str) -> None:
        # Favorited texts are pinned in their own list already — don't
        # duplicate them into the rotating history where they could be
        # displaced by newer sends.
        if text in self._saved_settings.announcement_favorites:
            return
        history = [t for t in self._saved_settings.announcement_history if t != text]
        history.insert(0, text)
        self._saved_settings.announcement_history = history[:ANNOUNCEMENT_HISTORY_MAX]
        self._save_current_settings()
