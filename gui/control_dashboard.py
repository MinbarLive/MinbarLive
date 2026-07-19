"""V3 control-dashboard chrome and factual readiness presentation.

This module deliberately owns presentation only.  It never starts or stops the
pipeline and it never consumes subtitle text.  ``AppGUI`` remains the lifecycle
owner; the dashboard merely reflects settings and the existing window state.
"""

from __future__ import annotations

import ctypes
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageFont

from config import RESOURCE_DIR
from providers import get_streaming_key_provider
from utils.settings import language_display_name

# Segoe Fluent Icons is the native Windows icon library.  The MDL2 fallback
# uses the same code points for the subset below.  Buttons remain text-labelled
# so the interface is still understandable when neither font exists.
ICON_FONT = "Segoe Fluent Icons" if sys.platform == "win32" else "Segoe UI Symbol"
ICONS = {
    "microphone": "\ue720",
    "speech": "\ue8d4",
    "translate": "\ue8c1",
    "display": "\ue7f4",
    "history": "\ue81c",
    "file": "\ue8e5",
    "announcement": "\ue789",
    "settings": "\ue713",
    "close": "\ue711",
    "diagnostics": "\ue9d9",
    "chevron_down": "\ue70d",
    "chevron_up": "\ue70e",
    "swap": "\ue8ab",
    "play": "\ue768",
    "stop": "\ue71a",
    "key": "\ue8d7",
    "screen": "\ue7f4",
    "add": "\ue710",
    "remove": "\ue738",
    "favorite": "\ue734",
    "favorite_filled": "\ue735",
}

_ICON_FONT_PATH = r"C:\Windows\Fonts\SegoeIcons.ttf"

LOGO_PATH = os.path.join(RESOURCE_DIR, "public", "MinbarLive_white_stroke.png")


def _system_reduced_motion() -> bool:
    """Return Windows' client-animation preference when it is available."""

    if sys.platform != "win32":
        return False
    try:
        enabled = ctypes.c_int(1)
        # SPI_GETCLIENTAREAANIMATION
        ok = ctypes.windll.user32.SystemParametersInfoW(
            0x1042, 0, ctypes.byref(enabled), 0
        )
        return bool(ok) and not bool(enabled.value)
    except Exception:
        return False


def provider_display_name(provider_id: str) -> str:
    provider_id = get_streaming_key_provider(provider_id)
    return {
        "gemini": "Google Gemini",
        "openai": "OpenAI",
        "anthropic": "Anthropic Claude",
        "deepgram": "Deepgram",
    }.get(provider_id, provider_id.replace("_", " ").title())


@dataclass(frozen=True)
class _StageState:
    label: str
    ready: bool
    live: bool = False
    warning: bool = False
    disabled: bool = False


class DashboardChrome:
    """Build and refresh the V3-only chrome around AppGUI's existing controls."""

    def __init__(self, host) -> None:
        self.host = host
        self._motion_jobs: set[str] = set()
        self._reduced_motion = _system_reduced_motion()
        self._stage_widgets: list[dict[str, object]] = []
        self._connectors: list[ctk.CTkFrame] = []
        self._icon_images: dict[tuple[str, int], ctk.CTkImage] = {}

    def icon_font(self, size: int, weight: str = "normal") -> ctk.CTkFont:
        return ctk.CTkFont(family=ICON_FONT, size=size, weight=weight)

    def icon_image(self, icon: str, size: int = 18) -> ctk.CTkImage | None:
        """Render one native Fluent glyph as a DPI-aware CTk image."""

        key = (icon, size)
        if key in self._icon_images:
            return self._icon_images[key]
        if icon not in ICONS or not os.path.exists(_ICON_FONT_PATH):
            return None

        canvas_size = size * 2

        def _render(color: str) -> Image.Image:
            image = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            font = ImageFont.truetype(_ICON_FONT_PATH, round(size * 1.45))
            glyph = ICONS[icon]
            bounds = draw.textbbox((0, 0), glyph, font=font)
            width = bounds[2] - bounds[0]
            height = bounds[3] - bounds[1]
            x = ((canvas_size - width) / 2) - bounds[0]
            y = ((canvas_size - height) / 2) - bounds[1]
            draw.text((x, y), glyph, font=font, fill=color)
            return image

        result = ctk.CTkImage(
            light_image=_render("#10212b"),
            dark_image=_render("#f5f1e8"),
            size=(size, size),
        )
        self._icon_images[key] = result
        return result

    def nav_button(
        self,
        parent,
        *,
        icon: str,
        text_key: str,
        fallback: str,
        command,
    ) -> ctk.CTkButton:
        h = self.host
        label = h.gui_texts.get(text_key, fallback)
        symbol = ICONS[icon]
        icon_image = self.icon_image(icon, 17)
        button = ctk.CTkButton(
            parent,
            text=label if icon_image is not None else f"{symbol}  {label}",
            image=icon_image,
            compound="left",
            command=command,
            width=92,
            height=38,
            corner_radius=12,
            border_width=0,
            font=ctk.CTkFont(family="Segoe UI Variable Text", size=12, weight="bold"),
            fg_color="transparent",
            hover_color=h._colors["button_hover"],
            text_color=h._colors["text"],
        )
        button._text_key = text_key  # type: ignore[attr-defined]
        button._symbol = None if icon_image is not None else symbol  # type: ignore[attr-defined]
        button._v3_icon = icon  # type: ignore[attr-defined]
        h._buttons.append(button)
        return button

    def build_signal_path(self, parent) -> ctk.CTkFrame:
        h = self.host
        band = ctk.CTkFrame(
            parent,
            height=116,
            corner_radius=0,
            fg_color=h._colors["sidebar"],
            border_width=0,
        )
        band.grid_propagate(False)
        band.grid_columnconfigure(0, weight=1)

        # The signal path deliberately uses the same solid surface as the
        # surrounding dashboard.  Depth belongs to the four factual status
        # cards below, not to a decorative backdrop behind them.
        rail = ctk.CTkFrame(band, fg_color="transparent", corner_radius=0)
        rail.place(relx=0.5, rely=0.5, relwidth=0.985, relheight=0.86, anchor="center")
        self._signal_rail = rail

        stage_defs = (
            ("microphone", "v3_stage_microphone", "Mikrofon"),
            ("speech", "v3_stage_recognition", "Spracherkennung"),
            ("translate", "v3_stage_translation", "Übersetzung"),
            ("display", "v3_stage_output", "Untertitelfenster"),
        )
        for column in range(7):
            rail.grid_columnconfigure(column, weight=1 if column % 2 == 0 else 0)
        rail.grid_rowconfigure(0, weight=1)

        for index, (icon_key, title_key, fallback) in enumerate(stage_defs):
            column = index * 2
            # Two nested surfaces create a restrained physical hierarchy:
            # a 3 px lower/right shadow, a crisp border and a one-pixel top
            # highlight.  No gradients or decorative texture are involved.
            stage_shadow = ctk.CTkFrame(
                rail,
                fg_color=h._colors["shadow"],
                corner_radius=17,
            )
            stage_shadow.grid(
                row=0,
                column=column,
                sticky="nsew",
                padx=6,
                pady=(7, 4),
            )
            stage_shadow.grid_columnconfigure(0, weight=1)
            stage_shadow.grid_rowconfigure(0, weight=1)

            stage = ctk.CTkFrame(
                stage_shadow,
                fg_color=h._colors["panel"],
                border_color=h._colors["border"],
                border_width=1,
                corner_radius=17,
            )
            stage.grid(row=0, column=0, sticky="nsew", padx=(0, 2), pady=(0, 3))
            stage.grid_columnconfigure(1, weight=1)

            highlight = ctk.CTkFrame(
                stage,
                height=1,
                corner_radius=1,
                fg_color=h._colors["surface_highlight"],
            )
            highlight.place(relx=0.5, rely=0.04, relwidth=0.76, anchor="n")

            icon_shell = ctk.CTkFrame(
                stage,
                width=54,
                height=54,
                corner_radius=27,
                fg_color=h._colors["panel"],
                border_color=h._colors["border"],
                border_width=1,
            )
            icon_shell.grid(row=0, column=0, rowspan=2, padx=(8, 8), pady=8)
            icon_shell.grid_propagate(False)
            icon_label = ctk.CTkLabel(
                icon_shell,
                text=ICONS[icon_key],
                font=self.icon_font(25),
                text_color=h._colors["muted"],
            )
            icon_label.place(relx=0.5, rely=0.5, anchor="center")

            title = ctk.CTkLabel(
                stage,
                text=h.gui_texts.get(title_key, fallback),
                font=ctk.CTkFont(
                    family="Segoe UI Variable Text", size=14, weight="bold"
                ),
                text_color=h._colors["text"],
                anchor="w",
            )
            title.grid(row=0, column=1, sticky="sw")
            title._text_key = title_key  # type: ignore[attr-defined]
            h._labels.append(title)

            status_row = ctk.CTkFrame(stage, fg_color="transparent")
            status_row.grid(row=1, column=1, sticky="nw", padx=(0, 6))
            dot = ctk.CTkLabel(
                status_row,
                text="",
                width=9,
                height=9,
                corner_radius=5,
                fg_color=h._colors["muted"],
            )
            dot.pack(side="left", padx=(0, 6), pady=(5, 0))
            state = ctk.CTkLabel(
                status_row,
                text="",
                font=ctk.CTkFont(family="Segoe UI Variable Text", size=11),
                text_color=h._colors["muted"],
                anchor="w",
            )
            state.pack(side="left")
            h._muted_labels.append(state)
            self._stage_widgets.append(
                {
                    "shadow": stage_shadow,
                    "surface": stage,
                    "highlight": highlight,
                    "shell": icon_shell,
                    "icon": icon_label,
                    "dot": dot,
                    "state": state,
                }
            )

            if index < len(stage_defs) - 1:
                connector = ctk.CTkFrame(
                    rail,
                    width=46,
                    height=2,
                    corner_radius=1,
                    fg_color=h._colors["border"],
                )
                connector.grid(row=0, column=column + 1, padx=2)
                self._connectors.append(connector)

        self.refresh(animate=False)
        return band

    def apply_theme(self) -> None:
        """Repaint the signal path with the current host palette."""

        h = self.host
        if hasattr(h, "signal_band"):
            h.signal_band.configure(fg_color=h._colors["sidebar"])
        self._signal_rail.configure(fg_color="transparent")
        for widgets in self._stage_widgets:
            widgets["shadow"].configure(fg_color=h._colors["shadow"])
            widgets["surface"].configure(
                fg_color=h._colors["panel"], border_color=h._colors["border"]
            )
            widgets["highlight"].configure(fg_color=h._colors["surface_highlight"])

    def _states(self) -> list[_StageState]:
        h = self.host
        s = h._saved_settings
        running = bool(h._running)
        device_combo = getattr(h, "device_combo", None)
        device_ready = bool(getattr(h, "device_names", [])) and (
            device_combo is not None and device_combo.current() is not None
        )
        stt_key_provider = get_streaming_key_provider(s.transcription_provider)
        translation_provider = s.ai_provider
        stt_ready = h._key_available(stt_key_provider)
        translation_ready = h._key_available(translation_provider)
        subtitle_exists = bool(h.subtitle_window and h.subtitle_window.winfo_exists())
        subtitle_enabled = h._subtitle_output_is_enabled()

        configured = h.gui_texts.get("v3_configured", "Konfiguriert")
        missing = h.gui_texts.get("v3_key_missing", "Schlüssel fehlt")
        saved = h.gui_texts.get("v3_key_saved", "Schlüssel gespeichert")
        live = h.gui_texts.get("v3_live", "Live")
        error = h.gui_texts.get("v3_error", "Fehler")
        opens = h.gui_texts.get("v3_opens_on_start", "Öffnet bei Start")
        open_text = h.gui_texts.get("v3_window_open", "Fenster offen")
        output_disabled = h.gui_texts.get("v3_output_disabled", "Deaktiviert")
        no_device = h.gui_texts.get("v3_device_missing", "Gerät fehlt")

        runtime_errors = getattr(h, "_runtime_errors", {})
        device_error = "microphone" in runtime_errors
        stt_error = "transcription" in runtime_errors
        translation_error = "translation" in runtime_errors

        return (
            [
                _StageState(
                    error if device_error else (live if running else configured),
                    device_ready and not device_error,
                    live=running and not device_error,
                ),
                _StageState(
                    error
                    if stt_error
                    else (live if running else (saved if stt_ready else missing)),
                    stt_ready and not stt_error,
                    live=running and not stt_error,
                ),
                _StageState(
                    error
                    if translation_error
                    else (
                        live if running else (saved if translation_ready else missing)
                    ),
                    translation_ready and not translation_error,
                    live=running and not translation_error,
                ),
                (
                    _StageState(output_disabled, False, disabled=True)
                    if not subtitle_enabled
                    else _StageState(
                        live
                        if running
                        else (open_text if subtitle_exists else opens),
                        subtitle_exists or bool(s.hide_subtitle_on_stop),
                        live=running,
                    )
                ),
            ]
            if device_ready
            else [
                _StageState(no_device, False),
                _StageState(saved if stt_ready else missing, stt_ready),
                _StageState(saved if translation_ready else missing, translation_ready),
                (
                    _StageState(output_disabled, False, disabled=True)
                    if not subtitle_enabled
                    else _StageState(
                        open_text if subtitle_exists else opens,
                        subtitle_exists or bool(s.hide_subtitle_on_stop),
                    )
                ),
            ]
        )

    def _paint_stage(self, index: int, stage: _StageState) -> None:
        h = self.host
        widgets = self._stage_widgets[index]
        if stage.disabled:
            color = h._colors["muted"]
            soft = h._colors["panel_soft"]
        elif stage.live or stage.ready:
            color = h._colors["accent"]
            soft = h._colors["accent_soft"]
        elif stage.warning:
            color = h._colors["warning"]
            soft = h._colors["warning_soft"]
        else:
            color = h._colors["danger"]
            soft = h._colors["danger_soft"]
        widgets["shell"].configure(border_color=color, fg_color=soft)
        widgets["icon"].configure(text_color=color)
        widgets["dot"].configure(fg_color=color)
        widgets["state"].configure(text=stage.label, text_color=color)
        if index > 0:
            self._connectors[index - 1].configure(
                fg_color=h._colors["accent"] if stage.live else h._colors["border"]
            )

    def refresh(self, *, animate: bool | None = None) -> None:
        if not self._stage_widgets:
            return
        self.cancel_motion()
        states = self._states()
        running = bool(self.host._running)
        if animate is None:
            animate = running
        if running and animate and not self._reduced_motion:
            for index, stage in enumerate(states):
                self._schedule_motion_job(
                    index * 45,
                    lambda i=index, st=stage: self._complete_stage_job(i, st),
                )
        else:
            for index, stage in enumerate(states):
                self._paint_stage(index, stage)
        self._refresh_summary(states)

    def _complete_stage_job(self, index: int, stage: _StageState) -> None:
        self._paint_stage(index, stage)

    def animate_transition(
        self,
        step: Callable[[float], None],
        *,
        duration_ms: int = 160,
        on_complete: Callable[[], None] | None = None,
    ) -> None:
        """Run a short deterministic layout transition respecting OS motion."""

        self.cancel_motion()
        if self._reduced_motion:
            step(1.0)
            if on_complete is not None:
                on_complete()
            return

        step(0.0)
        frame_count = 4
        for frame in range(1, frame_count + 1):
            progress = frame / frame_count

            def _paint(value: float = progress, final: bool = frame == frame_count):
                step(value)
                if final and on_complete is not None:
                    on_complete()

            self._schedule_motion_job(round(duration_ms * progress), _paint)

    def _schedule_motion_job(self, delay_ms: int, callback: Callable[[], None]) -> None:
        holder: list[str] = []

        def _run() -> None:
            if holder:
                self._motion_jobs.discard(holder[0])
            callback()

        job = self.host.after(delay_ms, _run)
        holder.append(job)
        self._motion_jobs.add(job)

    def animate_card_reflow(self, widgets: list[object], *, wide: bool) -> None:
        """Give responsive card changes a quiet 160 ms vertical settle."""

        final_tops = (8, 8, 8) if wide else (8, 8, 8)
        final_bottoms = (12, 12, 12) if wide else (8, 8, 14)

        def _step(progress: float) -> None:
            for index, widget in enumerate(widgets):
                top = round(final_tops[index] + (1.0 - progress) * 10)
                widget.grid_configure(pady=(top, final_bottoms[index]))

        self.animate_transition(_step)

    def animate_drawer_open(self, drawer: object) -> None:
        """Slide the already-mapped diagnostic drawer into its final inset."""

        def _step(progress: float) -> None:
            left = round(52 - (34 * progress))
            drawer.grid_configure(padx=(left, 22), pady=20)

        self.animate_transition(_step)

    def _refresh_summary(self, states: list[_StageState]) -> None:
        h = self.host
        if not hasattr(h, "action_summary_label"):
            return
        missing_providers: list[str] = []
        for provider in (
            get_streaming_key_provider(h._saved_settings.transcription_provider),
            h._saved_settings.ai_provider,
        ):
            if not h._key_available(provider) and provider not in missing_providers:
                missing_providers.append(provider)
        runtime_error = getattr(h, "_runtime_error_message", None)
        if runtime_error:
            summary = runtime_error
            h.action_summary_label.configure(text_color=h._colors["danger"])
        elif h._running:
            summary = h.gui_texts.get("v3_session_running", "Sitzung läuft")
            h.action_summary_label.configure(text_color=h._colors["muted"])
        elif missing_providers:
            names = ", ".join(provider_display_name(p) for p in missing_providers)
            summary = h.gui_texts.get(
                "v3_missing_keys_summary", "Schlüssel fehlt: {providers}"
            ).format(providers=names)
            h.action_summary_label.configure(text_color=h._colors["muted"])
        else:
            target = language_display_name(h._saved_settings.target_language)
            if h._subtitle_output_is_enabled():
                monitor = h.selected_screen_index + 1
                summary = h.gui_texts.get(
                    "v3_ready_summary", "Bereit · {target} · Monitor {monitor}"
                ).format(target=target, monitor=monitor)
            else:
                summary = h.gui_texts.get(
                    "v3_ready_summary_no_screen",
                    "Bereit · {target} · Kein Bildschirm",
                ).format(target=target)
            h.action_summary_label.configure(text_color=h._colors["muted"])
        h.action_summary_label.configure(text=summary)

        if not h._running:
            rejected_provider = getattr(h, "_rejected_key_provider", None)
            if rejected_provider:
                provider_name = provider_display_name(rejected_provider)
                label = h.gui_texts.get(
                    "v3_replace_provider_key", "Replace {provider} key"
                ).format(provider=provider_name)

                def command(p=rejected_provider):
                    h._prompt_provider_key(p)

            else:
                label = h.gui_texts.get(
                    "v3_complete_setup" if missing_providers else "v3_start_live",
                    "Setup abschließen" if missing_providers else "Live starten",
                )
                command = h.on_start
            h.start_btn.configure(text=label, command=command)
            h.primary_action_btn.configure(text=label, command=command)

    def cancel_motion(self) -> None:
        for job in list(self._motion_jobs):
            try:
                self.host.after_cancel(job)
            except Exception:
                pass
        self._motion_jobs.clear()
