"""Batch / SRT-generation window (mixin for AppGUI).

Moved verbatim out of gui/app_gui.py: the Batch/File Toplevel (file picker,
per-run language/provider/model dropdowns, progress + worker-thread queue
polling). Batch STATE lives on the AppGUI instance and survives the window
being opened/closed — the widgets are rebuilt each open, so every widget
update is guarded by ``_batch_win_exists()`` and mirrored into state for
the next open. Uses AppGUI services: ``_colors``, ``gui_texts``,
``_combo`` / ``_label`` (register=False), ``_set_toplevel_icon``,
``_saved_settings`` and ``_prompt_provider_key``. The window is rebuilt
from scratch on open; theme/GUI-language changes close it.
"""

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk

import customtkinter as ctk

from gui.control_dashboard import ICON_FONT, ICONS
from gui.scaling import centered_position
from providers import (
    PROVIDER_CHOICES,
    TRANSCRIPTION_PROVIDER_CHOICES,
    get_default_model,
    get_model_choices,
    has_usable_key,
    ranked_keyed_provider,
)
from utils.logging import log
from utils.settings import (
    SOURCE_LANGUAGES,
    STREAMING_TRANSCRIPTION_PROVIDERS,
    TARGET_LANGUAGE_NAMES,
    language_canonical_name,
    language_display_name,
)

# Streaming engines can't run in batch — the realtime engines fall back to
# their segmented sibling (same API key).
_BATCH_STT_FALLBACKS = {
    "openai_realtime": "openai",
    "gemini_realtime": "gemini",
}


def _batch_stt_fallback(provider_id: str) -> str:
    """Segmented sibling for a streaming engine. Deepgram has no segmented
    mode and no shared key family — pick the highest-ranked segmented STT
    provider the user actually holds a key for."""
    if provider_id == "deepgram":
        return ranked_keyed_provider(["gemini", "openai"])
    return _BATCH_STT_FALLBACKS.get(provider_id, provider_id)


class BatchViewMixin:
    """Batch/File window + batch worker plumbing, hosted by AppGUI."""

    def _init_batch_state(self) -> None:
        """Batch state lives on the controller so it survives the Batch window
        being opened/closed (the widgets are rebuilt each open)."""
        self._batch_input_path: str | None = None
        self._batch_thread: threading.Thread | None = None
        self._batch_cancel_event: threading.Event | None = None
        self._batch_queue: queue.Queue = queue.Queue()
        self._batch_progress_value: float = 0.0
        self._batch_status_text: str = ""
        self._batch_status_color: str = self._colors["muted"]
        # Per-run overrides (None until the dropdowns are built/changed); the
        # batch job picks its own languages/engine independently of the live app.
        self._batch_source_language: str | None = None
        self._batch_target_language: str | None = None
        self._batch_transcription_provider: str | None = None
        self._batch_transcription_model: str | None = None
        self._batch_translation_provider: str | None = None
        self._batch_translation_model: str | None = None
        # Default to both deliverables: writing a second small text file is
        # free (no extra API call), so a normal user always gets subtitles AND
        # a plain transcript without having to think about the format.
        self._batch_output_format: str = "both"
        # Bilingual SRT: original transcription above the translation in every
        # subtitle block (any language pair). On by default; untick for a clean
        # single-language subtitle file (e.g. an OBS overlay).
        self._batch_bilingual_srt: bool = True
        # Folder the last successful run wrote to (for the "Open folder" button).
        self._batch_last_output_dir: str | None = None
        # Engine/model dropdowns live behind a collapsed "More settings"
        # expander — most users only ever touch file + languages + output.
        self._batch_more_open: bool = False

    def _batch_win_exists(self) -> bool:
        return (
            getattr(self, "_batch_win", None) is not None
            and self._batch_win.winfo_exists()
        )

    def _close_batch_window(self) -> None:
        if self._batch_win_exists():
            self._batch_win.destroy()
        self._batch_win = None

    def _open_batch_window(self) -> None:
        if self._batch_win_exists():
            self._batch_win.lift()
            self._batch_win.focus()
            return

        win = ctk.CTkToplevel(self)
        win.title(self.gui_texts.get("batch_file", "Batch / File"))
        win.resizable(False, False)
        win.configure(fg_color=self._colors["app_bg"])
        # Transparent while building + sizing to hide the flash, then fade in.
        # Alpha (not withdraw) so the transient window is never unmapped.
        try:
            win.attributes("-alpha", 0.0)
        except tk.TclError:
            pass

        win.after(200, lambda: self._set_toplevel_icon(win))
        win.transient(self)
        self._batch_win = win
        self._build_batch_widgets(win)
        self._resize_batch_window(recenter=True)
        try:
            win.attributes("-alpha", 1.0)
        except tk.TclError:
            pass

    def _resize_batch_window(self, recenter: bool) -> None:
        """Size the window to the content's natural height (varies with GUI
        language and the More-settings expander state).

        winfo_reqheight() is physical px, but CTkToplevel.geometry() scales
        WxH by the window (DPI) factor, so convert to the logical units it
        expects — otherwise the window comes out DPI-times too tall.
        On expander toggles the position the user dragged the window to is
        kept (recenter=False); +X+Y pass through unscaled.
        """
        if not self._batch_win_exists():
            return
        win = self._batch_win
        win.update_idletasks()
        w = 480
        scaling = ctk.ScalingTracker.get_window_scaling(win)
        h = int(win.winfo_reqheight() / scaling) + 1
        if recenter:
            x, y = centered_position(self, w, h)
        else:
            x = win.winfo_x()
            y = win.winfo_y()
        win.geometry(f"{w}x{h}+{x}+{y}")

    def _build_batch_widgets(self, parent: ctk.CTkBaseClass) -> None:
        card = ctk.CTkFrame(
            parent,
            fg_color=self._colors["card"],
            border_color=self._colors["border"],
            border_width=2,
            corner_radius=24,
        )
        card.pack(fill="both", expand=True, padx=16, pady=16)
        card.grid_columnconfigure(0, weight=1, uniform="batch_actions")
        card.grid_columnconfigure(1, weight=1, uniform="batch_actions")

        header = ctk.CTkFrame(card, fg_color="transparent")
        header.grid(row=0, column=0, columnspan=2, sticky="ew", padx=20, pady=(18, 4))
        header.grid_columnconfigure(1, weight=1)
        symbol = ctk.CTkLabel(
            header,
            text=ICONS["file"],
            font=ctk.CTkFont(family=ICON_FONT, size=20),
            text_color=self._colors["accent"],
            width=44,
            height=44,
            fg_color=self._colors["panel_soft"],
            corner_radius=16,
        )
        symbol.grid(row=0, column=0, rowspan=2, sticky="nw", padx=(0, 12), pady=(1, 0))
        title = ctk.CTkLabel(
            header,
            text=self.gui_texts.get("batch_file", "Batch / File"),
            font=ctk.CTkFont(family="Segoe UI", size=20, weight="bold"),
            text_color=self._colors["text"],
            anchor="w",
        )
        title.grid(row=0, column=1, sticky="ew")
        subtitle = ctk.CTkLabel(
            header,
            text=self.gui_texts.get("batch_file_sub", ""),
            font=ctk.CTkFont(family="Segoe UI", size=13),
            text_color=self._colors["muted"],
            anchor="w",
            justify="left",
            wraplength=340,
        )
        subtitle.grid(row=1, column=1, sticky="ew", pady=(2, 0))

        self._build_batch_options(card)

        # File row: the picker button itself shows the chosen file's name, with
        # an ✕ beside it to clear the selection and pick another.
        running = self._batch_thread is not None
        file_row = ctk.CTkFrame(card, fg_color="transparent")
        file_row.grid(row=2, column=0, columnspan=2, sticky="ew", padx=18, pady=(14, 12))
        file_row.grid_columnconfigure(0, weight=1)
        file_row.grid_columnconfigure(1, weight=0)
        self.batch_pick_btn = ctk.CTkButton(
            file_row,
            text=self._batch_file_button_text(),
            command=self._on_batch_pick_file,
            width=0,
            height=40,
            corner_radius=14,
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            fg_color=self._colors["button"],
            hover_color=self._colors["button_hover"],
            text_color="#ffffff",
            anchor="w" if self._batch_input_path else "center",
            state="disabled" if running else "normal",
        )
        self.batch_pick_btn.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.batch_clear_btn = ctk.CTkButton(
            file_row,
            text="",
            image=self._dashboard.icon_image("close", 16),
            command=self._on_batch_clear_file,
            width=40,
            height=40,
            corner_radius=14,
            fg_color=self._colors["button"],
            hover_color=self._colors["danger"],
            text_color=self._colors["text"],
            state="disabled" if running else "normal",
        )
        self.batch_clear_btn.grid(row=0, column=1)
        if not self._batch_input_path:
            self.batch_clear_btn.grid_remove()

        self.batch_progress = ctk.CTkProgressBar(
            card,
            height=10,
            progress_color=self._colors["accent"],
            fg_color=self._colors["panel_soft"],
        )
        self.batch_progress.set(self._batch_progress_value)
        self.batch_progress.grid(
            row=3, column=0, columnspan=2, sticky="ew", padx=18, pady=(6, 10)
        )

        self.batch_status_label = ctk.CTkLabel(
            card,
            text=self._batch_status_text,
            font=ctk.CTkFont(family="Segoe UI", size=13),
            text_color=self._batch_status_color,
            anchor="w",
            justify="left",
        )
        self.batch_status_label.grid(
            row=4, column=0, columnspan=2, sticky="ew", padx=18, pady=(0, 10)
        )

        self.batch_start_btn = ctk.CTkButton(
            card,
            text=self.gui_texts.get("batch_start", "Start"),
            command=self._on_batch_start,
            width=0,
            height=44,
            corner_radius=14,
            font=ctk.CTkFont(family="Segoe UI", size=15, weight="bold"),
            fg_color=self._colors["accent"],
            hover_color=self._colors["accent_hover"],
            text_color="#ffffff",
            text_color_disabled=self._colors["muted"],
            state="normal" if (self._batch_input_path and not running) else "disabled",
        )
        self.batch_start_btn.grid(
            row=5, column=0, sticky="ew", padx=(18, 6), pady=(0, 16)
        )

        self.batch_cancel_btn = ctk.CTkButton(
            card,
            text=self.gui_texts.get("batch_cancel", "Cancel"),
            command=self._on_batch_cancel,
            width=0,
            height=44,
            corner_radius=14,
            font=ctk.CTkFont(family="Segoe UI", size=15, weight="bold"),
            fg_color=self._colors["button"],
            hover_color=self._colors["button_hover"],
            text_color="#ffffff",
            text_color_disabled=self._colors["muted"],
            state="normal" if running else "disabled",
        )
        self.batch_cancel_btn.grid(
            row=5, column=1, sticky="ew", padx=(6, 18), pady=(0, 16)
        )

        # "Show in history" — batch runs are also recorded in the history
        # viewer's Batch tab (browse / re-export / summarise past runs).
        self.batch_history_btn = ctk.CTkButton(
            card,
            text=self.gui_texts.get("batch_open_history", "Show in history"),
            command=self._on_batch_open_history,
            width=0,
            height=36,
            corner_radius=12,
            font=ctk.CTkFont(family="Segoe UI", size=13),
            fg_color=self._colors["button"],
            hover_color=self._colors["button_hover"],
            text_color=self._colors["text"],
        )
        self.batch_history_btn.grid(
            row=6, column=0, sticky="ew", padx=(18, 6), pady=(2, 18)
        )
        # "Open folder" — jumps to where the run wrote the .srt/.txt (next to
        # the source file). Enabled once a run has finished this session.
        self.batch_open_folder_btn = ctk.CTkButton(
            card,
            text=self.gui_texts.get("batch_open_folder", "Open folder"),
            command=self._on_batch_open_folder,
            width=0,
            height=36,
            corner_radius=12,
            font=ctk.CTkFont(family="Segoe UI", size=13),
            fg_color=self._colors["button"],
            hover_color=self._colors["button_hover"],
            text_color=self._colors["text"],
            text_color_disabled=self._colors["muted"],
            state="normal" if self._batch_last_output_dir else "disabled",
        )
        self.batch_open_folder_btn.grid(
            row=6, column=1, sticky="ew", padx=(6, 18), pady=(2, 18)
        )

    def _build_batch_options(self, card: ctk.CTkBaseClass) -> None:
        """Per-run language, provider and model dropdowns. The batch job is
        configured independently of the live app — nothing here changes the
        main settings.

        Batch always runs the *segmented* transcription engine, so Deepgram
        (real-time only) is not offered here; the OpenAI/Gemini models shown are
        what the run actually uses."""
        s = self._saved_settings
        box = ctk.CTkFrame(card, fg_color="transparent")
        box.grid(row=1, column=0, columnspan=2, sticky="ew", padx=18, pady=(8, 8))
        box.grid_columnconfigure(0, weight=1, uniform="batch_eng")
        box.grid_columnconfigure(1, weight=1, uniform="batch_eng")

        # ── Source + target language (independent of the live app) ───────────
        self._label(
            box, "batch_source_language", size=12, weight="bold", register=False
        ).grid(row=0, column=0, sticky="w", pady=(0, 1))
        self._label(
            box, "batch_target_language", size=12, weight="bold", register=False
        ).grid(row=0, column=1, sticky="w", pady=(0, 1))
        # Canonical (English) names are stored/passed to the pipeline; the
        # dropdowns show the native endonym via language_display_name().
        source_names = [name for name, _code in SOURCE_LANGUAGES]
        self._batch_source_language = (
            self._batch_source_language
            if self._batch_source_language in source_names
            else (s.source_language if s.source_language in source_names else source_names[0])
        )
        self.batch_source_combo = self._combo(
            box,
            values=[language_display_name(n) for n in source_names],
            command=lambda _v: self._on_batch_source_language_change(),
            register=False,
        )
        self.batch_source_combo.set(language_display_name(self._batch_source_language))
        self.batch_source_combo.grid(
            row=1, column=0, sticky="ew", padx=(0, 6), pady=(0, 14)
        )
        target_names = list(TARGET_LANGUAGE_NAMES)
        self._batch_target_language = (
            self._batch_target_language
            if self._batch_target_language in target_names
            else (s.target_language if s.target_language in target_names else target_names[0])
        )
        self.batch_target_combo = self._combo(
            box,
            values=[language_display_name(n) for n in target_names],
            command=lambda _v: self._on_batch_target_language_change(),
            register=False,
        )
        self.batch_target_combo.set(language_display_name(self._batch_target_language))
        self.batch_target_combo.grid(
            row=1, column=1, sticky="ew", padx=(6, 0), pady=(0, 14)
        )

        # ── Output format (the primary deliverable — kept visible, not behind
        #    "More settings"). Defaults to Both; the extra file costs nothing.
        self._label(
            box, "batch_output_format", size=12, weight="bold", register=False
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 1))
        self._batch_output_ids = ["srt", "txt", "both"]
        self.batch_output_combo = self._combo(
            box,
            values=[
                self.gui_texts.get("batch_output_srt", "Subtitles (.srt)"),
                self.gui_texts.get("batch_output_text", "Transcript (.txt)"),
                self.gui_texts.get("batch_output_both", "Both (.srt + .txt)"),
            ],
            command=lambda _v: self._on_batch_output_format_change(),
            register=False,
        )
        if self._batch_output_format not in self._batch_output_ids:
            self._batch_output_format = "both"
        self.batch_output_combo.current(
            self._batch_output_ids.index(self._batch_output_format)
        )
        self.batch_output_combo.grid(
            row=3, column=0, columnspan=2, sticky="ew", pady=(0, 10)
        )

        # ── Bilingual SRT toggle (original + translation per block) ──────────
        # Sits under the output picker it affects. Greyed out when the output
        # is transcript-only (no SRT is written then).
        self.batch_bilingual_check = ctk.CTkCheckBox(
            box,
            text=self.gui_texts.get(
                "batch_bilingual_srt", "Bilingual subtitles (original + translation)"
            ),
            command=self._on_batch_bilingual_toggle,
            height=30,
            font=ctk.CTkFont(family="Segoe UI", size=13),
            corner_radius=8,
            checkbox_width=22,
            checkbox_height=22,
            border_width=2,
            fg_color=self._colors["accent"],
            hover_color=self._colors["accent_hover"],
            border_color=self._colors["entry_border"],
            text_color=self._colors["text"],
        )
        if self._batch_bilingual_srt:
            self.batch_bilingual_check.select()
        else:
            self.batch_bilingual_check.deselect()
        self.batch_bilingual_check.grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(0, 14)
        )
        self._update_bilingual_checkbox_state()

        # ── "More settings" expander (engines + Default) ─────────────────────
        # Collapsed by default: most users only touch file + languages + output;
        # the run still prompts for any missing key on Start.
        self.batch_more_btn = ctk.CTkButton(
            box,
            text=self._batch_more_button_text(),
            image=self._dashboard.icon_image(
                "chevron_up" if self._batch_more_open else "chevron_down", 15
            ),
            compound="left",
            command=self._on_batch_toggle_more,
            width=0,
            height=32,
            corner_radius=12,
            font=ctk.CTkFont(family="Segoe UI", size=13),
            fg_color=self._colors["button"],
            hover_color=self._colors["button_hover"],
            text_color=self._colors["text"],
            anchor="w",
        )
        self.batch_more_btn.grid(row=5, column=0, columnspan=2, sticky="ew")

        more = ctk.CTkFrame(box, fg_color="transparent")
        more.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        more.grid_columnconfigure(0, weight=1, uniform="batch_more")
        more.grid_columnconfigure(1, weight=1, uniform="batch_more")
        self._batch_more_frame = more
        if not self._batch_more_open:
            more.grid_remove()

        # ── Transcription: provider (segmented only) + model ─────────────────
        self._label(
            more, "batch_transcription_model", size=12, weight="bold", register=False
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 1))
        stt_provider_choices = [
            (n, p)
            for n, p in TRANSCRIPTION_PROVIDER_CHOICES
            if p not in STREAMING_TRANSCRIPTION_PROVIDERS
        ]
        self._batch_stt_provider_ids = [p for _n, p in stt_provider_choices]
        eff_stt = _batch_stt_fallback(s.transcription_provider)
        if eff_stt not in self._batch_stt_provider_ids:
            eff_stt = self._batch_stt_provider_ids[0]
        self._batch_transcription_provider = self._pick_batch_provider(
            self._batch_transcription_provider, eff_stt, self._batch_stt_provider_ids
        )
        self.batch_stt_provider_combo = self._combo(
            more,
            values=[n for n, _p in stt_provider_choices],
            command=lambda _v: self._on_batch_transcription_provider_change(),
            register=False,
        )
        self.batch_stt_provider_combo.current(
            self._batch_stt_provider_ids.index(self._batch_transcription_provider)
        )
        self.batch_stt_provider_combo.grid(
            row=1, column=0, sticky="ew", padx=(0, 6), pady=(0, 14)
        )
        self.batch_stt_combo = self._combo(
            more,
            values=[],
            command=lambda _v: self._on_batch_transcription_model_change(),
            register=False,
        )
        self.batch_stt_combo.grid(row=1, column=1, sticky="ew", padx=(6, 0), pady=(0, 14))
        self._populate_batch_stt_models(reset=False)

        # ── Translation: provider + model ────────────────────────────────────
        self._label(
            more, "batch_translation_model", size=12, weight="bold", register=False
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 1))
        self._batch_trans_provider_ids = [p for _n, p in PROVIDER_CHOICES]
        self._batch_translation_provider = self._pick_batch_provider(
            self._batch_translation_provider, s.ai_provider, self._batch_trans_provider_ids
        )
        self.batch_trans_provider_combo = self._combo(
            more,
            values=[n for n, _p in PROVIDER_CHOICES],
            command=lambda _v: self._on_batch_translation_provider_change(),
            register=False,
        )
        self.batch_trans_provider_combo.current(
            self._batch_trans_provider_ids.index(self._batch_translation_provider)
        )
        self.batch_trans_provider_combo.grid(
            row=3, column=0, sticky="ew", padx=(0, 6), pady=(0, 14)
        )
        self.batch_trans_combo = self._combo(
            more,
            values=[],
            command=lambda _v: self._on_batch_translation_model_change(),
            register=False,
        )
        self.batch_trans_combo.grid(
            row=3, column=1, sticky="ew", padx=(6, 0), pady=(0, 14)
        )
        self._populate_batch_trans_models(reset=False)

        # ── Reset engines + output to the app defaults ───────────────────────
        # Sits in the right column, aligned under the model combo above it — a
        # minor "reset" action kept to half the width so it doesn't read as a
        # primary, full-width control.
        self.batch_defaults_btn = ctk.CTkButton(
            more,
            text=self.gui_texts.get("batch_defaults", "Use Default"),
            command=self._on_batch_defaults,
            width=0,
            height=32,
            corner_radius=12,
            font=ctk.CTkFont(family="Segoe UI", size=13),
            fg_color=self._colors["button"],
            hover_color=self._colors["button_hover"],
            text_color=self._colors["text"],
        )
        self.batch_defaults_btn.grid(
            row=4, column=1, sticky="ew", padx=(6, 0), pady=(2, 0)
        )

    def _batch_more_button_text(self) -> str:
        return self.gui_texts.get("batch_more_settings", "More settings")

    def _on_batch_toggle_more(self) -> None:
        self._batch_more_open = not self._batch_more_open
        if not self._batch_win_exists():
            return
        if self._batch_more_open:
            self._batch_more_frame.grid()
        else:
            self._batch_more_frame.grid_remove()
        self.batch_more_btn.configure(
            text=self._batch_more_button_text(),
            image=self._dashboard.icon_image(
                "chevron_up" if self._batch_more_open else "chevron_down", 15
            ),
        )
        self._resize_batch_window(recenter=False)

    def _on_batch_defaults(self) -> None:
        """Reset engines + output format to what a fresh open would derive
        from the app settings, discarding this session's per-run overrides."""
        if self._batch_thread is not None:
            return
        s = self._saved_settings
        eff_stt = _batch_stt_fallback(s.transcription_provider)
        if eff_stt not in self._batch_stt_provider_ids:
            eff_stt = self._batch_stt_provider_ids[0]
        self._batch_transcription_provider = eff_stt
        self._batch_transcription_model = None
        self.batch_stt_provider_combo.current(
            self._batch_stt_provider_ids.index(eff_stt)
        )
        self._populate_batch_stt_models(reset=False)

        trans = (
            s.ai_provider
            if s.ai_provider in self._batch_trans_provider_ids
            else self._batch_trans_provider_ids[0]
        )
        self._batch_translation_provider = trans
        self._batch_translation_model = None
        self.batch_trans_provider_combo.current(
            self._batch_trans_provider_ids.index(trans)
        )
        self._populate_batch_trans_models(reset=False)

        self._batch_output_format = "both"
        self.batch_output_combo.current(self._batch_output_ids.index("both"))

        self._batch_bilingual_srt = True
        self.batch_bilingual_check.select()
        self._update_bilingual_checkbox_state()

    def _on_batch_output_format_change(self) -> None:
        idx = self.batch_output_combo.current()
        if idx is not None and 0 <= idx < len(self._batch_output_ids):
            self._batch_output_format = self._batch_output_ids[idx]
        self._update_bilingual_checkbox_state()

    def _on_batch_bilingual_toggle(self) -> None:
        self._batch_bilingual_srt = bool(self.batch_bilingual_check.get())

    def _update_bilingual_checkbox_state(self) -> None:
        """Grey the bilingual toggle when the output is transcript-only — it
        only affects the SRT. Skipped while a run holds the controls locked."""
        if not self._batch_win_exists() or self._batch_thread is not None:
            return
        state = "disabled" if self._batch_output_format == "txt" else "normal"
        self.batch_bilingual_check.configure(state=state)

    def _populate_batch_stt_models(self, reset: bool) -> None:
        pid = self._batch_transcription_provider
        choices = get_model_choices(pid, "transcription")
        self._batch_stt_ids = [mid for _n, mid in choices]
        default = get_default_model(pid, "transcription")
        model = (
            default
            if reset
            else self._pick_batch_model(
                self._batch_transcription_model,
                self._saved_settings.transcription_model,
                self._batch_stt_ids,
                default,
            )
        )
        if model not in self._batch_stt_ids:
            model = self._batch_stt_ids[0]
        self._batch_transcription_model = model
        self.batch_stt_combo.configure(values=[n for n, _m in choices])
        self.batch_stt_combo.current(self._batch_stt_ids.index(model))

    def _populate_batch_trans_models(self, reset: bool) -> None:
        pid = self._batch_translation_provider
        choices = get_model_choices(pid, "translation")
        self._batch_trans_ids = [mid for _n, mid in choices]
        default = get_default_model(pid, "translation")
        model = (
            default
            if reset
            else self._pick_batch_model(
                self._batch_translation_model,
                self._saved_settings.translation_model,
                self._batch_trans_ids,
                default,
            )
        )
        if model not in self._batch_trans_ids:
            model = self._batch_trans_ids[0]
        self._batch_translation_model = model
        self.batch_trans_combo.configure(values=[n for n, _m in choices])
        self.batch_trans_combo.current(self._batch_trans_ids.index(model))

    @staticmethod
    def _pick_batch_provider(
        current: str | None, from_settings: str, ids: list[str]
    ) -> str:
        for candidate in (current, from_settings):
            if candidate in ids:
                return candidate
        return ids[0]

    @staticmethod
    def _pick_batch_model(
        current: str | None, from_settings: str, ids: list[str], default: str
    ) -> str:
        """Preselect a batch model: keep a still-valid prior batch choice, else
        the configured model if the provider offers it, else the provider
        default, else the first available (ids is never empty)."""
        for candidate in (current, from_settings, default):
            if candidate in ids:
                return candidate
        return ids[0]

    def _on_batch_transcription_provider_change(self) -> None:
        idx = self.batch_stt_provider_combo.current()
        if idx is None or not (0 <= idx < len(self._batch_stt_provider_ids)):
            return
        self._batch_transcription_provider = self._batch_stt_provider_ids[idx]
        self._populate_batch_stt_models(reset=True)

    def _on_batch_translation_provider_change(self) -> None:
        idx = self.batch_trans_provider_combo.current()
        if idx is None or not (0 <= idx < len(self._batch_trans_provider_ids)):
            return
        self._batch_translation_provider = self._batch_trans_provider_ids[idx]
        self._populate_batch_trans_models(reset=True)

    def _on_batch_source_language_change(self) -> None:
        self._batch_source_language = language_canonical_name(
            self.batch_source_combo.get()
        )

    def _on_batch_target_language_change(self) -> None:
        self._batch_target_language = language_canonical_name(
            self.batch_target_combo.get()
        )

    def _on_batch_transcription_model_change(self) -> None:
        idx = self.batch_stt_combo.current()
        if idx is not None and 0 <= idx < len(self._batch_stt_ids):
            self._batch_transcription_model = self._batch_stt_ids[idx]

    def _on_batch_translation_model_change(self) -> None:
        idx = self.batch_trans_combo.current()
        if idx is not None and 0 <= idx < len(self._batch_trans_ids):
            self._batch_translation_model = self._batch_trans_ids[idx]

    def _batch_option_combos(self) -> list:
        """Every batch-config control, locked together while a run is in
        progress."""
        return [
            self.batch_source_combo,
            self.batch_target_combo,
            self.batch_stt_provider_combo,
            self.batch_stt_combo,
            self.batch_trans_provider_combo,
            self.batch_trans_combo,
            self.batch_output_combo,
            self.batch_bilingual_check,
        ]

    def _on_batch_open_history(self) -> None:
        self._open_history_window("batch")

    def _on_batch_open_folder(self) -> None:
        """Open the folder the last run wrote to (next to the source file)."""
        folder = self._batch_last_output_dir
        if not folder or not os.path.isdir(folder):
            return
        try:
            if sys.platform == "win32":
                os.startfile(folder)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except OSError as e:
            log(f"BATCH Open folder failed: {e}", level="WARNING")

    # ── Batch / SRT generation ────────────────────────────────────────────
    # State lives on the controller; the Batch window (and its widgets) may be
    # opened/closed at any time, so every widget update is guarded by
    # _batch_win_exists() and mirrored into state for the next open.

    def _set_batch_progress(self, value: float) -> None:
        self._batch_progress_value = value
        if self._batch_win_exists():
            self.batch_progress.set(value)

    def _set_batch_status(self, text: str, color: str) -> None:
        self._batch_status_text = text
        self._batch_status_color = color
        if self._batch_win_exists():
            self.batch_status_label.configure(text=text, text_color=color)

    def _batch_required_key_providers(self) -> list[str]:
        """Batch needs a key for the chosen translation + STT providers. Falls
        back to settings if the batch dropdowns aren't built yet; streaming
        engines can't run in batch, so they map to their segmented sibling."""
        translation = self._batch_translation_provider or self._saved_settings.ai_provider
        stt = self._batch_transcription_provider
        if not stt:
            stt = _batch_stt_fallback(self._saved_settings.transcription_provider)
        result: list[str] = []
        for p in (translation, stt):
            if p and p not in result:
                result.append(p)
        return result

    def _on_batch_pick_file(self) -> None:
        from tkinter import filedialog

        path = filedialog.askopenfilename(
            parent=self._batch_win if self._batch_win_exists() else self,
            title=self.gui_texts.get("batch_pick_file", "Choose file…"),
            filetypes=[
                (
                    self.gui_texts.get("batch_media_files", "Audio/Video"),
                    "*.wav *.mp3 *.m4a *.aac *.flac *.ogg *.opus "
                    "*.mp4 *.mkv *.mov *.webm *.avi *.m4v *.wmv *.flv "
                    "*.ts *.mpg *.mpeg",
                ),
                (self.gui_texts.get("batch_all_files", "All files"), "*.*"),
            ],
        )
        if not path:
            return
        self._batch_input_path = path
        self._set_batch_progress(0)
        self._set_batch_status("", self._colors["muted"])
        self._refresh_batch_file_row()

    def _batch_file_button_text(self) -> str:
        """Picker button label: the chosen file's name or the 'choose a file'
        prompt. Long names are truncated in the middle so the start AND the
        extension stay visible (e.g. 'The episode is out now, catch…gone.m4a')."""
        if not self._batch_input_path:
            return self.gui_texts.get("batch_pick_file", "Choose file…")
        name = os.path.basename(self._batch_input_path)
        limit = 48
        if len(name) <= limit:
            return name
        stem, ext = os.path.splitext(name)
        head = max(10, limit - len(ext) - 1)  # room for the "…" + extension
        return stem[:head].rstrip() + "…" + ext

    def _refresh_batch_file_row(self) -> None:
        """Sync the picker button text, the ✕ clear button, and the Start
        button to the current file selection."""
        if not self._batch_win_exists():
            return
        self.batch_pick_btn.configure(
            text=self._batch_file_button_text(),
            anchor="w" if self._batch_input_path else "center",
        )
        if self._batch_input_path:
            self.batch_clear_btn.grid()
        else:
            self.batch_clear_btn.grid_remove()
        if self._batch_thread is None:
            self.batch_start_btn.configure(
                state="normal" if self._batch_input_path else "disabled"
            )

    def _on_batch_clear_file(self) -> None:
        if self._batch_thread is not None:
            return
        self._batch_input_path = None
        self._set_batch_progress(0)
        self._set_batch_status("", self._colors["muted"])
        self._refresh_batch_file_row()

    def _on_batch_start(self) -> None:
        if not self._batch_input_path or self._batch_thread is not None:
            return
        for provider in self._batch_required_key_providers():
            if not has_usable_key(provider):
                self._prompt_provider_key(provider)
                if not has_usable_key(provider):
                    return

        # Imported lazily: batch pulls in the translation pipeline (and with
        # it the RAG embeddings), which GUI startup should not pay for.
        from batch.processor import (
            FfmpegNotFoundError,
            is_ffmpeg_available,
            process_file,
        )

        if (
            not self._batch_input_path.lower().endswith(".wav")
            and not is_ffmpeg_available()
        ):
            # Offer a one-time consented download instead of a dead end
            # (Windows only — other platforms have a package manager).
            if sys.platform == "win32" and self._offer_ffmpeg_download():
                return  # the run restarts automatically after the download
            self._set_batch_status(
                self.gui_texts.get(
                    "batch_ffmpeg_missing",
                    "ffmpeg not found — install ffmpeg to process this "
                    "file format.",
                ),
                self._colors["danger"],
            )
            return

        self._batch_cancel_event = threading.Event()
        if self._batch_win_exists():
            self.batch_start_btn.configure(state="disabled")
            self.batch_pick_btn.configure(state="disabled")
            self.batch_clear_btn.configure(state="disabled")
            self.batch_cancel_btn.configure(state="normal")
            for combo in self._batch_option_combos():
                combo.configure(state="disabled")
        self._set_batch_progress(0)
        self._set_batch_status("…", self._colors["muted"])

        def _worker(
            path=self._batch_input_path,
            cancel=self._batch_cancel_event,
            src_lang=self._batch_source_language,
            tgt_lang=self._batch_target_language,
            stt_provider=self._batch_transcription_provider,
            stt_model=self._batch_transcription_model,
            trans_provider=self._batch_translation_provider,
            trans_model=self._batch_translation_model,
            out_format=self._batch_output_format,
            bilingual=self._batch_bilingual_srt,
        ):
            try:
                out = process_file(
                    path,
                    progress_callback=lambda done, total: self._batch_queue.put(
                        ("progress", done, total)
                    ),
                    cancel_event=cancel,
                    transcription_provider=stt_provider,
                    transcription_model=stt_model,
                    translation_provider=trans_provider,
                    translation_model=trans_model,
                    source_language=src_lang,
                    target_language=tgt_lang,
                    output_format=out_format,
                    bilingual_srt=bilingual,
                )
                self._batch_queue.put(("done", out))
            except FfmpegNotFoundError:
                self._batch_queue.put(("error", "__ffmpeg__"))
            except Exception as exc:
                self._batch_queue.put(("error", str(exc)))

        self._batch_thread = threading.Thread(
            target=_worker, daemon=True, name="batch-processor"
        )
        self._batch_thread.start()
        log(f"BATCH Started: {self._batch_input_path}", level="INFO")
        self.after(200, self._poll_batch_queue)

    def _offer_ffmpeg_download(self) -> bool:
        """Ask to download ffmpeg into the app data dir (one time, consented).

        Returns True when a download was started — the batch run the user
        asked for re-triggers itself automatically once ffmpeg is in place.
        Returns False when the user declined (caller shows the manual
        install message as before).
        """
        from utils.ffmpeg_download import FFMPEG_DOWNLOAD_MB

        parent = self._batch_win if self._batch_win_exists() else self
        prompt = self.gui_texts.get(
            "batch_ffmpeg_download_prompt",
            "ffmpeg is required to convert this file format. Download it "
            "now? (one time, ~{mb} MB)",
        ).format(mb=FFMPEG_DOWNLOAD_MB)
        if not self._confirm("ffmpeg", prompt, parent=parent):
            return False

        self._batch_cancel_event = threading.Event()
        if self._batch_win_exists():
            self.batch_start_btn.configure(state="disabled")
            self.batch_pick_btn.configure(state="disabled")
            self.batch_clear_btn.configure(state="disabled")
            self.batch_cancel_btn.configure(state="normal")
            for combo in self._batch_option_combos():
                combo.configure(state="disabled")
        self._set_batch_progress(0)

        def _worker(cancel=self._batch_cancel_event):
            from utils.ffmpeg_download import (
                FfmpegDownloadCancelled,
                download_ffmpeg,
            )

            try:
                download_ffmpeg(
                    progress_cb=lambda pct: self._batch_queue.put(
                        ("dl_progress", pct)
                    ),
                    cancel_event=cancel,
                )
                self._batch_queue.put(("dl_done", None))
            except FfmpegDownloadCancelled:
                # Reuses the regular cancelled-run status path
                self._batch_queue.put(("done", None))
            except Exception as exc:
                self._batch_queue.put(("error", str(exc)))

        self._batch_thread = threading.Thread(
            target=_worker, daemon=True, name="ffmpeg-download"
        )
        self._batch_thread.start()
        log("BATCH ffmpeg download started", level="INFO")
        self.after(200, self._poll_batch_queue)
        return True

    def _on_batch_cancel(self) -> None:
        if self._batch_cancel_event is not None:
            self._batch_cancel_event.set()
        if self._batch_win_exists():
            self.batch_cancel_btn.configure(state="disabled")

    def _finish_batch(self) -> None:
        self._batch_thread = None
        self._batch_cancel_event = None
        if self._batch_win_exists():
            self.batch_cancel_btn.configure(state="disabled")
            self.batch_pick_btn.configure(state="normal")
            self.batch_clear_btn.configure(state="normal")
            for combo in self._batch_option_combos():
                combo.configure(state="normal")
            # Re-apply the transcript-only greying the blanket re-enable above
            # would otherwise clobber.
            self._update_bilingual_checkbox_state()
            if self._batch_input_path:
                self.batch_start_btn.configure(state="normal")

    def _poll_batch_queue(self) -> None:
        try:
            while True:
                msg = self._batch_queue.get_nowait()
                kind = msg[0]
                if kind == "progress":
                    _, done, total = msg
                    self._set_batch_progress(done / max(1, total))
                    template = self.gui_texts.get(
                        "batch_progress", "Segment {current}/{total}"
                    )
                    self._set_batch_status(
                        template.format(current=done, total=total),
                        self._colors["muted"],
                    )
                elif kind == "dl_progress":
                    pct = msg[1]
                    self._set_batch_progress(pct / 100)
                    template = self.gui_texts.get(
                        "batch_ffmpeg_downloading", "Downloading ffmpeg… {percent}%"
                    )
                    self._set_batch_status(
                        template.format(percent=pct), self._colors["muted"]
                    )
                elif kind == "dl_done":
                    self._finish_batch()
                    # ffmpeg is in place — start the run the user asked for
                    self._on_batch_start()
                    return
                elif kind == "done":
                    self._finish_batch()
                    out = msg[1]
                    if out:
                        self._batch_last_output_dir = os.path.dirname(out)
                        self._set_batch_progress(1)
                        template = self.gui_texts.get(
                            "batch_done", "Saved next to your file: {name}"
                        )
                        self._set_batch_status(
                            template.format(name=os.path.basename(out)),
                            self._colors["accent"],
                        )
                        # Enable "Open folder" and draw the eye to where the run
                        # can be reopened in-app.
                        if self._batch_win_exists():
                            self.batch_open_folder_btn.configure(state="normal")
                            self.batch_history_btn.configure(
                                fg_color=self._colors["accent"],
                                text_color="#ffffff",
                            )
                        log(f"BATCH Done: {out}", level="INFO")
                    else:
                        self._set_batch_status(
                            self.gui_texts.get("batch_cancelled", "Cancelled"),
                            self._colors["warning"],
                        )
                    return
                elif kind == "error":
                    self._finish_batch()
                    if msg[1] == "__ffmpeg__":
                        text = self.gui_texts.get(
                            "batch_ffmpeg_missing",
                            "ffmpeg not found — install ffmpeg to process "
                            "this file format.",
                        )
                    else:
                        template = self.gui_texts.get(
                            "batch_error", "Failed: {error}"
                        )
                        text = template.format(error=msg[1])
                    self._set_batch_status(text, self._colors["danger"])
                    log(f"BATCH Failed: {msg[1]}", level="ERROR")
                    return
        except queue.Empty:
            pass
        if self._batch_thread is not None:
            self.after(200, self._poll_batch_queue)

