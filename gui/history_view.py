"""Session history viewer + summary dialog (mixin for AppGUI).

Moved verbatim out of gui/app_gui.py: the History | Batch | Log viewer
window and the "Summarise session" dialog. The methods run on the AppGUI
instance (``self``) and use its services: ``_colors``, ``gui_texts``,
``_combo`` / ``_alert`` / ``_confirm`` / ``_set_toplevel_icon`` (from
WidgetFactoryMixin), ``_saved_settings`` and ``_prompt_provider_key``.
Both windows are rebuilt from scratch on open; theme/GUI-language changes
close them (see AppGUI._apply_theme / _update_all_ui_texts).
"""

import os
import queue
import threading
from datetime import datetime

import customtkinter as ctk

from gui.scaling import centered_position
from providers import PROVIDER_CHOICES, has_usable_key
from utils.cost_tracking import (
    delete_cost_session,
    format_usd,
    list_cost_sessions,
)
from utils.history import (
    BatchRun,
    HistorySession,
    LogFile,
    batch_srt_path,
    list_batch_runs,
    list_history_sessions,
    list_log_files,
    pair_entries,
    parse_history_file,
    read_batch_languages,
    read_summary,
    summary_path,
    write_summary,
)
from utils.logging import log
from utils.settings import (
    TARGET_LANGUAGE_NAMES,
    language_canonical_name,
    language_display_name,
)


class HistoryViewMixin:
    """History viewer window + summary dialog, hosted by AppGUI."""

    def _history_win_exists(self) -> bool:
        return (
            getattr(self, "_history_win", None) is not None
            and self._history_win.winfo_exists()
        )

    def _close_history_window(self) -> None:
        self._close_summary_window()
        if self._history_win_exists():
            self._history_win.destroy()
        self._history_win = None

    def _open_history_window(self, initial_tab: str = "history") -> None:
        if self._history_win_exists():
            self._history_win.lift()
            self._history_win.focus()
            if initial_tab == self._history_active_tab:
                self._render_history_tab()
            else:
                self._switch_history_tab(initial_tab)
            return

        win = ctk.CTkToplevel(self)
        win.title(self.gui_texts.get("history_title", "Session History"))
        win.configure(fg_color=self._colors["app_bg"])

        win.after(200, lambda: self._set_toplevel_icon(win))
        win.transient(self)
        self.update_idletasks()
        x, y = centered_position(self, 900, 560)
        win.geometry(f"900x560+{x}+{y}")
        self._history_win = win
        self._history_active_tab = initial_tab
        self._history_selected_session = None
        self._history_selected_log = None
        self._history_selected_batch = None
        self._history_selected_cost = None
        # Which format the batch preview currently shows ("srt"/"txt").
        self._history_batch_format: str | None = None

        # Layout: row 0 tab bar (full width), row 1 list | textbox, row 2 actions
        win.grid_columnconfigure(0, weight=0)
        win.grid_columnconfigure(1, weight=1)
        win.grid_rowconfigure(1, weight=1)

        # ── Tab bar: History | Log (History is the default) ──────────────────
        tab_bar = ctk.CTkFrame(win, fg_color=self._colors["sidebar"], height=48)
        tab_bar.grid(row=0, column=0, columnspan=2, sticky="ew")
        tab_bar.grid_propagate(False)
        self._history_tab_buttons = {}
        tabs = (
            ("history", "history_tab_sessions", "History"),
            ("batch", "history_tab_batch", "Batch"),
            ("costs", "history_tab_costs", "Costs"),
            ("logs", "history_tab_logs", "Log"),
        )
        for i, (tab, key, default) in enumerate(tabs):
            b = ctk.CTkButton(
                tab_bar,
                text=self.gui_texts.get(key, default),
                command=lambda t=tab: self._switch_history_tab(t),
                width=130,
                height=34,
                corner_radius=10,
                font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
                fg_color=self._colors["button"],
                hover_color=self._colors["button_hover"],
                text_color=self._colors["text"],
            )
            b.pack(side="left", padx=(16 if i == 0 else 6, 0), pady=8)
            self._history_tab_buttons[tab] = b

        # ── List (left) — populated per active tab ───────────────────────────
        self._history_list = ctk.CTkScrollableFrame(
            win, fg_color=self._colors["sidebar"], corner_radius=0, width=280
        )
        self._history_list.grid(row=1, column=0, rowspan=2, sticky="nsw")
        self._history_list.grid_columnconfigure(0, weight=1)

        # ── Content (right): optional format toolbar + preview textbox ───────
        content = ctk.CTkFrame(win, fg_color=self._colors["log_bg"], corner_radius=0)
        content.grid(row=1, column=1, sticky="nsew")
        content.grid_columnconfigure(0, weight=1)
        content.grid_rowconfigure(1, weight=1)

        # SRT | TXT toggle — only shown for batch runs that offer both formats.
        self._history_format_bar = ctk.CTkFrame(
            content, fg_color=self._colors["sidebar"], height=40
        )
        self._history_format_bar.grid(row=0, column=0, sticky="ew")
        self._history_format_bar.grid_propagate(False)
        self._history_format_buttons = {}
        for i, fmt in enumerate(("srt", "txt")):
            b = ctk.CTkButton(
                self._history_format_bar,
                text=fmt.upper(),
                command=lambda f=fmt: self._on_history_format_select(f),
                width=64,
                height=28,
                corner_radius=9,
                font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
                fg_color=self._colors["button"],
                hover_color=self._colors["button_hover"],
                text_color=self._colors["text"],
            )
            b.pack(side="left", padx=(12 if i == 0 else 6, 0), pady=6)
            self._history_format_buttons[fmt] = b
        self._history_format_bar.grid_remove()  # shown per tab/selection

        self._history_textbox = ctk.CTkTextbox(
            content,
            fg_color=self._colors["log_bg"],
            text_color=self._colors["log_text"],
            font=ctk.CTkFont(family="Segoe UI", size=14),
            wrap="word",
            corner_radius=0,
            border_width=0,
            state="disabled",
        )
        self._history_textbox.grid(row=1, column=0, sticky="nsew")

        # ── Action bar (operates on the currently selected item) ─────────────
        action_bar = ctk.CTkFrame(win, fg_color=self._colors["sidebar"], height=56)
        action_bar.grid(row=2, column=1, sticky="ew")
        action_bar.grid_propagate(False)
        self._history_export_btn = ctk.CTkButton(
            action_bar,
            text=self.gui_texts.get("history_export", "Save…"),
            command=self._on_history_export,
            width=140,
            height=38,
            corner_radius=12,
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            fg_color=self._colors["button"],
            hover_color=self._colors["button_hover"],
            text_color=self._colors["text"],
        )
        self._history_export_btn.pack(side="right", padx=(6, 16), pady=9)
        self._history_copy_btn = ctk.CTkButton(
            action_bar,
            text=self.gui_texts.get("history_copy", "Copy"),
            command=self._on_history_copy,
            width=110,
            height=38,
            corner_radius=12,
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            fg_color=self._colors["button"],
            hover_color=self._colors["button_hover"],
            text_color=self._colors["text"],
        )
        self._history_copy_btn.pack(side="right", padx=6, pady=9)
        self._history_delete_btn = ctk.CTkButton(
            action_bar,
            text=self.gui_texts.get("history_delete", "Delete"),
            command=self._on_history_delete,
            width=110,
            height=38,
            corner_radius=12,
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            fg_color=self._colors["danger"],
            hover_color=self._colors["danger_hover"],
            text_color="#ffffff",
        )
        self._history_delete_btn.pack(side="right", padx=6, pady=9)

        # Summarise applies only to sessions → shown on the History tab only.
        self._history_summary_btn = ctk.CTkButton(
            action_bar,
            text=self.gui_texts.get("history_summarise", "Summarise"),
            command=self._on_history_summarise,
            width=160,
            height=38,
            corner_radius=12,
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            fg_color=self._colors["accent"],
            hover_color=self._colors["accent_hover"],
            text_color="#ffffff",
        )

        self._render_history_tab()

    def _switch_history_tab(self, tab: str) -> None:
        if tab == self._history_active_tab:
            return
        self._history_active_tab = tab
        self._render_history_tab()

    def _render_history_tab(self) -> None:
        """Style the tab buttons, rebuild the left list for the active tab, and
        show/hide the Summarise button (sessions only)."""
        for tab, btn in self._history_tab_buttons.items():
            active = tab == self._history_active_tab
            btn.configure(
                fg_color=self._colors["accent"]
                if active
                else self._colors["button"],
                text_color="#ffffff" if active else self._colors["text"],
            )
        for child in self._history_list.winfo_children():
            child.destroy()
        # The SRT|TXT toggle is a batch-only affordance; hide it here and let
        # _show_batch_run bring it back when a multi-format run is selected.
        self._history_format_bar.grid_remove()

        # History and Batch both hold transcripts → the Summarise button
        # applies to both; the raw Log tab has nothing to summarise.
        if self._history_active_tab == "history":
            self._history_summary_btn.pack(side="left", padx=(16, 6), pady=9)
            self._render_session_list()
        elif self._history_active_tab == "batch":
            self._history_summary_btn.pack(side="left", padx=(16, 6), pady=9)
            self._render_batch_list()
        elif self._history_active_tab == "costs":
            self._history_summary_btn.pack_forget()
            self._close_summary_window()
            self._render_cost_list()
        else:
            self._history_summary_btn.pack_forget()
            self._close_summary_window()
            self._render_log_list()

    @staticmethod
    def _ellipsize(text: str, limit: int = 40) -> str:
        """Middle-ellipsis so a long filename keeps its start AND its extension."""
        if len(text) <= limit:
            return text
        head = (limit - 1) * 2 // 3
        tail = limit - 1 - head
        return f"{text[:head]}…{text[-tail:]}" if tail else f"{text[: limit - 1]}…"

    def _add_history_row(
        self, index: int, title: str, subtitle: str, on_click, tag: str | None = None
    ):
        """A clickable list row: bold title (+ optional right-aligned tag) over a
        muted subtitle. Two separate labels — not one multi-line CTkButton — so a
        short/long title can never be clipped against the subtitle (the bug the
        single-button rows hit at fractional DPI)."""
        frame = ctk.CTkFrame(
            self._history_list, fg_color=self._colors["button"], corner_radius=14
        )
        frame.grid(
            row=index, column=0, sticky="ew", padx=10,
            pady=(10 if index == 0 else 4, 4),
        )
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_columnconfigure(1, weight=0)
        name = ctk.CTkLabel(
            frame,
            text=title,
            anchor="w",
            justify="left",
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            text_color=self._colors["text"],
            fg_color="transparent",
        )
        name.grid(row=0, column=0, sticky="ew", padx=(12, 6), pady=(8, 0))
        frame._name_label = name
        clickers = [frame, name]
        if tag:
            tag_lbl = ctk.CTkLabel(
                frame,
                text=tag,
                anchor="e",
                font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
                text_color=self._colors["muted"],
                fg_color="transparent",
            )
            tag_lbl.grid(row=0, column=1, sticky="e", padx=(0, 12), pady=(8, 0))
            frame._tag_label = tag_lbl
            clickers.append(tag_lbl)
        sub = ctk.CTkLabel(
            frame,
            text=subtitle,
            anchor="w",
            justify="left",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=self._colors["muted"],
            fg_color="transparent",
        )
        sub.grid(row=1, column=0, columnspan=2, sticky="ew", padx=12, pady=(0, 9))
        frame._sub_label = sub
        clickers.append(sub)
        for w in clickers:
            w.bind("<Button-1>", lambda _e: on_click())
        return frame

    def _select_history_row(self, frame, selected: bool) -> None:
        """Highlight the selected row (labels are transparent, so they follow
        the frame's fill; their text colour flips for contrast on the accent)."""
        frame.configure(
            fg_color=self._colors["accent"] if selected else self._colors["button"]
        )
        frame._name_label.configure(
            text_color="#ffffff" if selected else self._colors["text"]
        )
        frame._sub_label.configure(
            text_color="#ffffff" if selected else self._colors["muted"]
        )
        tag = getattr(frame, "_tag_label", None)
        if tag is not None:
            tag.configure(text_color="#ffffff" if selected else self._colors["muted"])

    def _format_duration(self, seconds: int) -> str:
        """Human-readable run length: seconds under a minute (so short clips
        don't all read '0 min'), whole minutes at or above one minute."""
        if seconds < 60:
            fmt = self.gui_texts.get("history_seconds", "{seconds} s")
            return fmt.format(seconds=seconds)
        fmt = self.gui_texts.get("history_minutes", "{minutes} min")
        return fmt.format(minutes=seconds // 60)

    def _render_session_list(self) -> None:
        sessions = list_history_sessions()
        self._history_session_buttons = []
        if not sessions:
            self._history_selected_session = None
            self._show_history_empty()
            return

        entries_fmt = self.gui_texts.get("history_entries", "{count} entries")
        for row, session in enumerate(sessions):
            detail = (
                f"{session.start_time}–{session.end_time}"
                f" ({self._format_duration(session.active_seconds)})"
                f" · {entries_fmt.format(count=session.entry_count)}"
            )
            # A 📝 prefix marks days that already have a saved AI summary.
            marker = "📝 " if session.has_summary else ""
            frame = self._add_history_row(
                row,
                title=f"{marker}{session.date} · {session.language_pair}",
                subtitle=detail,
                on_click=lambda s=session: self._show_history_session(s),
            )
            self._history_session_buttons.append((frame, session))

        self._show_history_session(sessions[0])

    def _render_log_list(self) -> None:
        logs = list_log_files()
        self._history_log_buttons = []
        if not logs:
            self._history_selected_log = None
            self._show_history_empty()
            return

        for row, logf in enumerate(logs):
            frame = self._add_history_row(
                row,
                title=logf.date,
                subtitle=f"{logf.size_kb} KB",
                on_click=lambda lf=logf: self._show_log_file(lf),
            )
            self._history_log_buttons.append((frame, logf))

        self._show_log_file(logs[0])

    def _render_batch_list(self) -> None:
        runs = list_batch_runs()
        self._history_batch_buttons = []
        if not runs:
            self._history_selected_batch = None
            self._show_history_empty()
            return

        entries_fmt = self.gui_texts.get("history_entries", "{count} entries")
        for row, run in enumerate(runs):
            # The formats this run holds (SRT / TXT / SRT+TXT) show as a
            # right-aligned tag on the name line, so the user can tell at a
            # glance what's available — and it can't be clipped off the end of
            # the (long) detail line the way an appended badge was.
            badge = "+".join(f.upper() for f in run.formats)
            detail = (
                f"{run.date} {run.time}"
                f" ({self._format_duration(run.active_seconds)})"
                f" · {run.language_pair}"
                f" · {entries_fmt.format(count=run.entry_count)}"
            )
            marker = "📝 " if run.has_summary else ""
            frame = self._add_history_row(
                row,
                title=f"{marker}{self._ellipsize(run.source_name)}",
                subtitle=detail,
                on_click=lambda r=run: self._show_batch_run(r),
                tag=badge or None,
            )
            self._history_batch_buttons.append((frame, run))

        self._show_batch_run(runs[0])

    @staticmethod
    def _local_datetime(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
        except (TypeError, ValueError):
            return None

    def _cost_duration_seconds(self, session: dict) -> int:
        start = self._local_datetime(session.get("started_at"))
        end = self._local_datetime(session.get("ended_at")) or datetime.now().astimezone()
        if start is None:
            return 0
        return max(0, int((end - start).total_seconds()))

    def _render_cost_list(self) -> None:
        sessions = list_cost_sessions()
        self._history_cost_buttons = []
        if not sessions:
            self._history_selected_cost = None
            self._show_history_empty()
            return
        for row, session in enumerate(sessions):
            started = self._local_datetime(session.get("started_at"))
            date_text = started.strftime("%d.%m.%Y · %H:%M") if started else "–"
            provider_names = [
                "OpenAI" if provider == "openai" else "Gemini" if provider == "gemini" else provider
                for provider in session.get("providers", {})
            ]
            subtitle = (
                f"{', '.join(provider_names) or '–'} · "
                f"{self._format_duration(self._cost_duration_seconds(session))}"
            )
            amount = format_usd(session.get("total_cost_usd", "0"))
            tag = f"≈ {amount}{'+' if not session.get('fully_priced', True) else ''}"
            frame = self._add_history_row(
                row,
                title=date_text,
                subtitle=subtitle,
                on_click=lambda s=session: self._show_cost_session(s),
                tag=tag,
            )
            self._history_cost_buttons.append((frame, session))
        self._show_cost_session(sessions[0])

    def _set_history_text(self, text: str) -> None:
        tb = self._history_textbox
        tb.configure(state="normal")
        tb.delete("1.0", "end")
        tb.insert("1.0", text)
        tb.configure(state="disabled")

    def _show_history_empty(self) -> None:
        if self._history_active_tab == "batch":
            msg = self.gui_texts.get("history_batch_empty", "No processed files yet")
        elif self._history_active_tab == "costs":
            msg = self.gui_texts.get("history_cost_empty", "No cost sessions yet")
        else:
            msg = self.gui_texts.get("history_empty", "No sessions recorded yet")
        self._set_history_text(msg)
        self._set_history_actions_enabled(False)

    def _set_history_actions_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self._history_export_btn.configure(state=state)
        self._history_copy_btn.configure(state=state)
        self._history_delete_btn.configure(state=state)
        self._history_summary_btn.configure(state=state)

    def _current_history_target(self) -> tuple[str, str] | None:
        """(path, date) of the selected item in the active tab, or None."""
        if self._history_active_tab == "history":
            session = getattr(self, "_history_selected_session", None)
            return (session.path, session.date) if session else None
        if self._history_active_tab == "batch":
            run = getattr(self, "_history_selected_batch", None)
            return (run.path, run.date) if run else None
        if self._history_active_tab == "costs":
            return None
        logf = getattr(self, "_history_selected_log", None)
        return (logf.path, logf.date) if logf else None

    def _show_history_session(self, session: HistorySession) -> None:
        self._history_selected_session = session
        for frame, s in self._history_session_buttons:
            self._select_history_row(frame, s is session)
        try:
            entries = parse_history_file(session.path)
        except OSError as e:
            log(f"History read error for {session.path}: {e}", level="WARNING")
            entries = []
        lines: list[str] = []
        for source, target in pair_entries(entries):
            lines.append(f"[{source.time}] {source.lang}:  {source.text}")
            # Same-language records: transcription == translation, show once.
            if target is not None and target.text != source.text:
                lines.append(f"           {target.lang}:  {target.text}")
            lines.append("")
        self._set_history_text("\n".join(lines))
        self._set_history_actions_enabled(True)

    def _show_log_file(self, logf: LogFile) -> None:
        self._history_selected_log = logf
        for frame, lf in self._history_log_buttons:
            self._select_history_row(frame, lf is logf)
        try:
            with open(logf.path, encoding="utf-8") as f:
                content = f.read()
        except OSError as e:
            log(f"Log read error for {logf.path}: {e}", level="WARNING")
            content = str(e)
        self._set_history_text(content)
        self._history_textbox.see("end")  # newest lines at the bottom
        self._set_history_actions_enabled(True)

    def _show_batch_run(self, run: BatchRun) -> None:
        self._history_selected_batch = run
        for frame, r in self._history_batch_buttons:
            self._select_history_row(frame, r is run)
        # Keep the current format if the run still offers it, else the first
        # available (SRT is listed first, so it wins when both exist).
        available = run.formats or ["txt"]
        if self._history_batch_format not in available:
            self._history_batch_format = available[0]
        self._render_batch_format_bar(available)
        self._set_history_text(self._batch_preview_text(run))
        self._set_history_actions_enabled(True)

    def _show_cost_session(self, session: dict) -> None:
        self._history_selected_cost = session
        for frame, item in self._history_cost_buttons:
            self._select_history_row(frame, item.get("id") == session.get("id"))

        started = self._local_datetime(session.get("started_at"))
        ended = self._local_datetime(session.get("ended_at"))
        date_text = started.strftime("%d.%m.%Y · %H:%M:%S") if started else "–"
        end_text = ended.strftime("%H:%M:%S") if ended else self.gui_texts.get(
            "v3_live", "Live"
        )
        total = format_usd(session.get("total_cost_usd", "0"))
        lines = [
            self.gui_texts.get("history_cost_estimate", "Estimated provider costs"),
            "",
            f"{self.gui_texts.get('history_cost_date', 'Date')}: {date_text} – {end_text}",
            f"{self.gui_texts.get('history_cost_duration', 'Duration')}: "
            f"{self._format_duration(self._cost_duration_seconds(session))}",
            f"{self.gui_texts.get('history_cost_total', 'Total')}: ≈ {total} USD"
            f"{' +' if not session.get('fully_priced', True) else ''}",
            "",
        ]
        role_names = {
            "translation": self.gui_texts.get("section_translation", "Translation"),
            "transcription": self.gui_texts.get("section_transcription", "Transcription"),
            "embedding": self.gui_texts.get("history_cost_embedding", "Verse matching"),
        }
        for provider_id, provider in session.get("providers", {}).items():
            provider_name = (
                "OpenAI" if provider_id == "openai" else "Gemini" if provider_id == "gemini" else provider_id
            )
            provider_cost = format_usd(provider.get("cost_usd", "0"))
            partial = not provider.get("fully_priced", True)
            lines.append(
                f"{provider_name} · ≈ {provider_cost}{' +' if partial else ''}"
            )
            for model, model_data in provider.get("models", {}).items():
                roles = ", ".join(
                    role_names.get(role, role) for role in model_data.get("roles", [])
                )
                model_cost = format_usd(model_data.get("cost_usd", "0"))
                lines.append(f"  {model} · {roles} · ≈ {model_cost}")
                usage = model_data.get("usage", {})
                input_tokens = sum(
                    int(usage.get(key, 0) or 0)
                    for key in (
                        "input_text_tokens",
                        "cached_input_text_tokens",
                        "input_audio_tokens",
                        "cached_input_audio_tokens",
                        "input_unknown_tokens",
                    )
                )
                output_tokens = sum(
                    int(usage.get(key, 0) or 0)
                    for key in (
                        "output_text_tokens",
                        "output_audio_tokens",
                        "output_unknown_tokens",
                    )
                )
                duration = float(usage.get("duration_seconds", 0) or 0)
                usage_bits = [
                    self.gui_texts.get(
                        "history_cost_tokens", "{input} input · {output} output tokens"
                    ).format(input=f"{input_tokens:,}", output=f"{output_tokens:,}")
                ]
                if duration:
                    usage_bits.append(f"{duration:.1f} s Audio")
                if not model_data.get("fully_priced", True):
                    usage_bits.append(
                        self.gui_texts.get("history_cost_partial", "partly unpriced")
                    )
                lines.append(f"    {' · '.join(usage_bits)}")
            lines.append("")

        lines.extend(
            (
                self.gui_texts.get(
                    "history_cost_notice",
                    "Paid Standard USD list price; free tier, discounts and taxes can differ.",
                ),
                f"{self.gui_texts.get('history_cost_pricing_date', 'Pricing as of')}: "
                f"{session.get('pricing_version', '–')}",
            )
        )
        self._set_history_text("\n".join(lines))
        self._set_history_actions_enabled(True)
        if session.get("status") == "active":
            self._history_delete_btn.configure(state="disabled")

    def _render_batch_format_bar(self, available: list[str]) -> None:
        """Show the SRT|TXT toggle only when a run offers both formats, and
        highlight the active one."""
        if len(available) < 2:
            self._history_format_bar.grid_remove()
            return
        self._history_format_bar.grid()
        for fmt, btn in self._history_format_buttons.items():
            active = fmt == self._history_batch_format
            btn.configure(
                fg_color=self._colors["accent"]
                if active
                else self._colors["button"],
                text_color="#ffffff" if active else self._colors["text"],
            )

    def _on_history_format_select(self, fmt: str) -> None:
        run = getattr(self, "_history_selected_batch", None)
        if run is None or fmt == self._history_batch_format:
            return
        if fmt not in (run.formats or []):
            return
        self._history_batch_format = fmt
        self._render_batch_format_bar(run.formats)
        self._set_history_text(self._batch_preview_text(run))

    def _batch_preview_text(self, run: BatchRun) -> str:
        """Exact SRT (from the stored sidecar) or the regenerated transcript."""
        if self._history_batch_format == "srt":
            try:
                with open(batch_srt_path(run.path), encoding="utf-8-sig") as f:
                    return f.read()
            except OSError as e:
                log(f"Batch SRT read error for {run.path}: {e}", level="WARNING")
                # Fall through to the transcript so the pane is never blank.
        return self._batch_transcript_text(run)

    def _batch_transcript_text(self, run: BatchRun) -> str:
        """Rebuild the plain transcript/translation document from the record —
        byte-identical to the .txt the run wrote next to the source file."""
        from batch.text_writer import build_text

        try:
            entries = parse_history_file(run.path)
        except OSError as e:
            log(f"Batch read error for {run.path}: {e}", level="WARNING")
            entries = []
        records = [
            (0.0, source.text, target.text if target is not None else source.text)
            for source, target in pair_entries(entries)
        ]
        langs = read_batch_languages(run.path)
        if langs:
            src_name, tgt_name = langs
        else:  # legacy record: fall back to the two-letter tags
            src_name = entries[0].lang if entries else ""
            tgt_name = entries[1].lang if len(entries) > 1 else ""
        return build_text(records, src_name, tgt_name)

    def _on_history_export(self) -> None:
        """Save a copy of the selected item (session or log) to a chosen path."""
        import shutil
        from tkinter import filedialog

        # Batch runs export the shown format (.srt or .txt), not the raw record.
        if self._history_active_tab == "batch":
            self._export_batch_current()
            return
        if self._history_active_tab == "costs":
            self._export_cost_current()
            return

        target_info = self._current_history_target()
        if target_info is None:
            return
        src_path, date = target_info
        is_log = self._history_active_tab == "logs"
        ext = ".log" if is_log else ".txt"
        parent = self._history_win if self._history_win_exists() else self
        target = filedialog.asksaveasfilename(
            parent=parent,
            title=self.gui_texts.get("history_export", "Save…"),
            defaultextension=ext,
            initialfile=f"MinbarLive_{date}{ext}",
            filetypes=[
                ("Log files" if is_log else "Text files", f"*{ext}"),
                (self.gui_texts.get("batch_all_files", "All files"), "*.*"),
            ],
        )
        if not target:
            return
        try:
            shutil.copyfile(src_path, target)
            log(f"Exported to {target}", level="INFO")
        except OSError as e:
            log(f"Export failed: {e}", level="ERROR")
            self._alert(
                self.gui_texts.get("history_export", "Save…"),
                str(e),
                parent=parent,
                danger=True,
            )

    def _export_cost_current(self) -> None:
        from tkinter import filedialog

        session = getattr(self, "_history_selected_cost", None)
        if session is None:
            return
        text = self._history_textbox.get("1.0", "end").rstrip("\n")
        started = self._local_datetime(session.get("started_at"))
        stamp = started.strftime("%Y-%m-%d_%H%M") if started else "session"
        parent = self._history_win if self._history_win_exists() else self
        target = filedialog.asksaveasfilename(
            parent=parent,
            title=self.gui_texts.get("history_export", "Save…"),
            defaultextension=".txt",
            initialfile=f"MinbarLive_Kosten_{stamp}.txt",
            filetypes=[
                ("Text files", "*.txt"),
                (self.gui_texts.get("batch_all_files", "All files"), "*.*"),
            ],
        )
        if not target:
            return
        try:
            with open(target, "w", encoding="utf-8-sig", newline="\n") as handle:
                handle.write(text + "\n")
        except OSError as exc:
            self._alert(
                self.gui_texts.get("history_export", "Save…"),
                str(exc),
                parent=parent,
                danger=True,
            )

    def _export_batch_current(self) -> None:
        """Write the currently shown batch format (.srt or .txt) to a file.
        The preview already holds the exact content, so we save what's shown."""
        from tkinter import filedialog

        run = getattr(self, "_history_selected_batch", None)
        if run is None:
            return
        text = self._history_textbox.get("1.0", "end").rstrip("\n")
        if not text.strip():
            return
        fmt = self._history_batch_format or "txt"
        ext = f".{fmt}"
        stem = os.path.splitext(run.source_name)[0] or f"MinbarLive_{run.date}"
        parent = self._history_win if self._history_win_exists() else self
        target = filedialog.asksaveasfilename(
            parent=parent,
            title=self.gui_texts.get("history_export", "Save…"),
            defaultextension=ext,
            initialfile=f"{stem}{ext}",
            filetypes=[
                ("SubRip subtitles" if fmt == "srt" else "Text files", f"*{ext}"),
                (self.gui_texts.get("batch_all_files", "All files"), "*.*"),
            ],
        )
        if not target:
            return
        try:
            # UTF-8 with BOM, matching what the batch writers produce.
            with open(target, "w", encoding="utf-8-sig", newline="\n") as f:
                f.write(text + "\n")
            log(f"Exported to {target}", level="INFO")
        except OSError as e:
            log(f"Export failed: {e}", level="ERROR")
            self._alert(
                self.gui_texts.get("history_export", "Save…"),
                str(e),
                parent=parent,
                danger=True,
            )

    def _on_history_copy(self) -> None:
        """Copy the current preview text to the clipboard (any tab)."""
        text = self._history_textbox.get("1.0", "end").strip()
        if not text:
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self._history_copy_btn.configure(
            text=self.gui_texts.get("history_copied", "Copied")
        )
        self.after(
            1500,
            lambda: self._history_copy_btn.winfo_exists()
            and self._history_copy_btn.configure(
                text=self.gui_texts.get("history_copy", "Copy")
            ),
        )

    def _on_history_delete(self) -> None:
        """Delete the selected item (with confirmation). Deleting a session
        also removes its saved-summary sidecar."""
        if self._history_active_tab == "costs":
            self._delete_cost_current()
            return
        target_info = self._current_history_target()
        if target_info is None:
            return
        src_path, _date = target_info
        parent = self._history_win if self._history_win_exists() else self
        if not self._confirm(
            self.gui_texts.get("history_delete", "Delete"),
            self.gui_texts.get(
                "history_delete_confirm",
                "Delete this history file? This cannot be undone.",
            ),
            parent=parent,
        ):
            return
        try:
            os.remove(src_path)
            if self._history_active_tab in ("history", "batch"):
                sidecar = summary_path(src_path)
                if os.path.exists(sidecar):
                    os.remove(sidecar)
            if self._history_active_tab == "batch":
                srt_sidecar = batch_srt_path(src_path)
                if os.path.exists(srt_sidecar):
                    os.remove(srt_sidecar)
            log(f"Deleted: {src_path}", level="INFO")
        except OSError as e:
            log(f"Delete failed: {e}", level="ERROR")
            self._alert(
                self.gui_texts.get("history_delete", "Delete"),
                str(e),
                parent=parent,
                danger=True,
            )
            return
        # Rebuild the active tab in place (keeps the tab selection)
        self._render_history_tab()

    def _delete_cost_current(self) -> None:
        session = getattr(self, "_history_selected_cost", None)
        if session is None or session.get("status") == "active":
            return
        parent = self._history_win if self._history_win_exists() else self
        if not self._confirm(
            self.gui_texts.get("history_delete", "Delete"),
            self.gui_texts.get(
                "history_cost_delete_confirm",
                "Delete this cost record? This cannot be undone.",
            ),
            parent=parent,
        ):
            return
        if not delete_cost_session(str(session.get("id", ""))):
            return
        self._render_history_tab()

    # ── Session summary ("Summarise" in the history viewer) ──────────────────
    def _summary_win_exists(self) -> bool:
        win = getattr(self, "_summary_win", None)
        return win is not None and win.winfo_exists()

    def _close_summary_window(self) -> None:
        if self._summary_win_exists():
            self._summary_win.destroy()
        self._summary_win = None
        self._summary_thread = None

    def _summary_provider_choices(self) -> list[tuple[str, str]]:
        """Translation providers that currently have a usable API key."""
        usable = [(name, pid) for name, pid in PROVIDER_CHOICES if has_usable_key(pid)]
        return usable or list(PROVIDER_CHOICES)

    def _on_history_summarise(self) -> None:
        # Both History sessions and Batch runs are history-format records, so
        # the same summary dialog works on either (BatchRun exposes the same
        # .path/.date/.language_pair/.has_summary the dialog reads).
        if self._history_active_tab == "batch":
            item = getattr(self, "_history_selected_batch", None)
        else:
            item = getattr(self, "_history_selected_session", None)
        if item is None:
            return
        if self._summary_win_exists():
            self._summary_win.lift()
            self._summary_win.focus()
            return
        self._open_summary_dialog(item)

    def _open_summary_dialog(self, session: HistorySession) -> None:
        win = ctk.CTkToplevel(self)
        win.title(self.gui_texts.get("summary_title", "Summarise session"))
        win.configure(fg_color=self._colors["app_bg"])
        win.after(200, lambda: self._set_toplevel_icon(win))
        win.transient(self._history_win if self._history_win_exists() else self)
        self.update_idletasks()
        w, h = 520, 430
        x = self.winfo_rootx() + (self.winfo_width() - w) // 2
        y = self.winfo_rooty() + (self.winfo_height() - h) // 2
        win.geometry(f"{w}x{h}+{x}+{y}")
        win.grid_columnconfigure(0, weight=1)
        win.grid_rowconfigure(4, weight=1)
        self._summary_win = win
        self._summary_session = session
        self._summary_queue: queue.Queue = queue.Queue()
        self._summary_thread = None

        pad = {"padx": 20}

        header = ctk.CTkLabel(
            win,
            text=f"{session.date} · {session.language_pair}",
            font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
            text_color=self._colors["muted"],
        )
        header.grid(row=0, column=0, sticky="w", pady=(18, 8), **pad)

        # Choices row: AI provider + summary language
        choices = ctk.CTkFrame(win, fg_color="transparent")
        choices.grid(row=1, column=0, sticky="ew", pady=(0, 10), **pad)
        choices.grid_columnconfigure(0, weight=1, uniform="sum")
        choices.grid_columnconfigure(1, weight=1, uniform="sum")

        prov_col = ctk.CTkFrame(choices, fg_color="transparent")
        prov_col.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        prov_col.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            prov_col,
            text=self.gui_texts.get("summary_provider", "AI provider"),
            font=ctk.CTkFont(family="Segoe UI", size=13),
            text_color=self._colors["text"],
        ).grid(row=0, column=0, sticky="w")
        provider_choices = self._summary_provider_choices()
        self._summary_provider_ids = [pid for _n, pid in provider_choices]
        self._summary_provider_combo = self._combo(
            prov_col,
            values=[name for name, _pid in provider_choices],
            register=False,
        )
        self._summary_provider_combo.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        last_prov = (
            self._saved_settings.last_summary_provider
            or self._saved_settings.ai_provider
        )
        if last_prov in self._summary_provider_ids:
            self._summary_provider_combo.current(
                self._summary_provider_ids.index(last_prov)
            )
        else:
            self._summary_provider_combo.current(0)

        lang_col = ctk.CTkFrame(choices, fg_color="transparent")
        lang_col.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        lang_col.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            lang_col,
            text=self.gui_texts.get("summary_language", "Summary language"),
            font=ctk.CTkFont(family="Segoe UI", size=13),
            text_color=self._colors["text"],
        ).grid(row=0, column=0, sticky="w")
        # Native endonyms in the dropdown, canonical English names everywhere
        # else (storage + the summary prompt) — same split as the language
        # dropdowns in the control panel and the wizard.
        self._summary_language_combo = self._combo(
            lang_col,
            values=[language_display_name(n) for n in TARGET_LANGUAGE_NAMES],
            register=False,
        )
        self._summary_language_combo.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        last_lang = language_canonical_name(
            self._saved_settings.last_summary_language
            or self._saved_settings.target_language
        )
        if last_lang not in TARGET_LANGUAGE_NAMES:
            last_lang = self._saved_settings.target_language
        self._summary_language_combo.set(language_display_name(last_lang))

        self._summary_generate_btn = ctk.CTkButton(
            win,
            text=self.gui_texts.get("summary_generate", "Create summary"),
            command=self._on_summary_generate,
            height=44,
            corner_radius=14,
            font=ctk.CTkFont(family="Segoe UI", size=15, weight="bold"),
            fg_color=self._colors["accent"],
            hover_color=self._colors["accent_hover"],
            text_color="#ffffff",
            text_color_disabled=self._colors["muted"],
        )
        self._summary_generate_btn.grid(row=2, column=0, sticky="ew", pady=(0, 8), **pad)

        self._summary_status = ctk.CTkLabel(
            win,
            text="",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=self._colors["muted"],
        )
        self._summary_status.grid(row=3, column=0, sticky="w", pady=(0, 4), **pad)

        self._summary_textbox = ctk.CTkTextbox(
            win,
            fg_color=self._colors["log_bg"],
            text_color=self._colors["log_text"],
            font=ctk.CTkFont(family="Segoe UI", size=14),
            wrap="word",
            corner_radius=12,
            border_width=0,
            state="disabled",
        )
        self._summary_textbox.grid(row=4, column=0, sticky="nsew", pady=(0, 8), **pad)

        # Bottom row: Save (to a file) + Copy (to clipboard), both right-aligned.
        btn_row = ctk.CTkFrame(win, fg_color="transparent")
        btn_row.grid(row=5, column=0, sticky="e", pady=(0, 16), **pad)
        self._summary_save_btn = ctk.CTkButton(
            btn_row,
            text=self.gui_texts.get("summary_save", "Save…"),
            command=self._on_summary_save,
            height=38,
            width=120,
            corner_radius=12,
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            fg_color=self._colors["button"],
            hover_color=self._colors["button_hover"],
            text_color=self._colors["text"],
            text_color_disabled=self._colors["muted"],
            state="disabled",
        )
        self._summary_save_btn.pack(side="left", padx=(0, 8))
        self._summary_copy_btn = ctk.CTkButton(
            btn_row,
            text=self.gui_texts.get("summary_copy", "Copy"),
            command=self._on_summary_copy,
            height=38,
            width=120,
            corner_radius=12,
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            fg_color=self._colors["button"],
            hover_color=self._colors["button_hover"],
            text_color=self._colors["text"],
            text_color_disabled=self._colors["muted"],
            state="disabled",
        )
        self._summary_copy_btn.pack(side="left")

        # A summary generated earlier is saved next to the history file — load it
        # so the user doesn't have to regenerate (the button still re-generates).
        existing = read_summary(session.path)
        if existing and existing.strip():
            self._set_summary_text(existing)
            self._summary_status.configure(
                text=self.gui_texts.get("summary_loaded", "Saved summary loaded"),
                text_color=self._colors["muted"],
            )
            self._summary_copy_btn.configure(state="normal")
            self._summary_save_btn.configure(state="normal")

    def _set_summary_text(self, text: str) -> None:
        box = self._summary_textbox
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("1.0", text)
        box.configure(state="disabled")

    def _on_summary_generate(self) -> None:
        if not self._summary_win_exists() or self._summary_thread is not None:
            return
        idx = self._summary_provider_combo.current()
        if idx is None or not (0 <= idx < len(self._summary_provider_ids)):
            return
        provider_id = self._summary_provider_ids[idx]
        # The dropdown shows the endonym; the prompt and the stored preference
        # take the canonical English name.
        language = language_canonical_name(self._summary_language_combo.get())

        if not has_usable_key(provider_id):
            self._prompt_provider_key(provider_id)
            if not has_usable_key(provider_id):
                return

        # Remember the choices for next time.
        self._saved_settings.last_summary_provider = provider_id
        self._saved_settings.last_summary_language = language
        self._save_current_settings()

        session = self._summary_session
        self._summary_generate_btn.configure(state="disabled")
        self._summary_copy_btn.configure(state="disabled")
        self._summary_save_btn.configure(state="disabled")
        self._summary_provider_combo.configure(state="disabled")
        self._summary_language_combo.configure(state="disabled")
        self._summary_status.configure(
            text=self.gui_texts.get("summary_generating", "Summarising…"),
            text_color=self._colors["muted"],
        )
        self._set_summary_text("")

        def _worker(
            path=session.path,
            label=f"{session.date} · {session.language_pair}",
            lang=language,
            pid=provider_id,
        ) -> None:
            from utils.session_summary import summarize_session_file

            try:
                text = summarize_session_file(
                    path,
                    target_language=lang,
                    provider_id=pid,
                    session_label=label,
                )
                self._summary_queue.put(("ok", text))
            except Exception as exc:
                self._summary_queue.put(("error", str(exc)))

        self._summary_thread = threading.Thread(
            target=_worker, daemon=True, name="session-summary"
        )
        self._summary_thread.start()
        log(f"SUMMARY started ({provider_id}, {language}): {session.path}", level="INFO")
        self.after(150, self._poll_summary_queue)

    def _poll_summary_queue(self) -> None:
        if not self._summary_win_exists():
            self._summary_thread = None
            return
        try:
            kind, payload = self._summary_queue.get_nowait()
        except queue.Empty:
            if self._summary_thread is not None:
                self.after(150, self._poll_summary_queue)
            return

        self._summary_thread = None
        self._summary_generate_btn.configure(state="normal")
        self._summary_provider_combo.configure(state="readonly")
        self._summary_language_combo.configure(state="readonly")
        if kind == "ok":
            self._set_summary_text(payload)
            self._summary_status.configure(text="")
            self._summary_copy_btn.configure(state="normal")
            self._summary_save_btn.configure(state="normal")
            # Persist next to the history file so it survives and marks the
            # session; also reflect the marker in the open list immediately.
            try:
                write_summary(self._summary_session.path, payload)
                self._mark_session_has_summary(self._summary_session)
            except OSError as e:
                log(f"SUMMARY persist failed: {e}", level="WARNING")
            log("SUMMARY done", level="INFO")
        else:
            template = self.gui_texts.get("summary_failed", "Summary failed: {error}")
            self._summary_status.configure(
                text=template.format(error=payload), text_color=self._colors["danger"]
            )
            log(f"SUMMARY failed: {payload}", level="ERROR")

    def _on_summary_copy(self) -> None:
        text = self._summary_textbox.get("1.0", "end").strip()
        if not text:
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self._summary_copy_btn.configure(
            text=self.gui_texts.get("summary_copied", "Copied")
        )
        self.after(
            1500,
            lambda: self._summary_copy_btn.winfo_exists()
            and self._summary_copy_btn.configure(
                text=self.gui_texts.get("summary_copy", "Copy")
            ),
        )

    def _on_summary_save(self) -> None:
        """Export the shown summary text to a user-chosen file."""
        from tkinter import filedialog

        text = self._summary_textbox.get("1.0", "end").strip()
        if not text:
            return
        session = self._summary_session
        parent = self._summary_win if self._summary_win_exists() else self
        target = filedialog.asksaveasfilename(
            parent=parent,
            title=self.gui_texts.get("summary_save", "Save…"),
            defaultextension=".txt",
            initialfile=f"MinbarLive_{session.date}_summary.txt",
            filetypes=[
                ("Text files", "*.txt"),
                (self.gui_texts.get("batch_all_files", "All files"), "*.*"),
            ],
        )
        if not target:
            return
        try:
            with open(target, "w", encoding="utf-8") as f:
                f.write(text)
            log(f"Summary exported to {target}", level="INFO")
        except OSError as e:
            log(f"Summary export failed: {e}", level="ERROR")
            self._alert(
                self.gui_texts.get("summary_save", "Save…"),
                str(e),
                parent=parent,
                danger=True,
            )
            return
        self._summary_save_btn.configure(
            text=self.gui_texts.get("summary_saved", "Saved")
        )
        self.after(
            1500,
            lambda: self._summary_save_btn.winfo_exists()
            and self._summary_save_btn.configure(
                text=self.gui_texts.get("summary_save", "Save…")
            ),
        )

    def _mark_session_has_summary(self, session) -> None:
        """Reflect a newly saved summary on the open list: flip the 📝 marker
        on the matching button (session or batch run) without rebuilding."""
        session.has_summary = True
        rows = getattr(self, "_history_session_buttons", []) + getattr(
            self, "_history_batch_buttons", []
        )
        for frame, s in rows:
            lbl = frame._name_label
            if s is session and "📝" not in lbl.cget("text"):
                lbl.configure(text=f"📝 {lbl.cget('text')}")
                break

