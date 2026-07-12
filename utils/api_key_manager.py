"""API key management: prompting, validation, and storage."""

from __future__ import annotations

import os
import sys
import tkinter as tk
from collections.abc import Callable

import customtkinter as ctk

from config import ICON_PATH, ICON_PATH_PNG
from providers import PROVIDER_CHOICES, clear_api_key, save_api_key
from utils.keyring_storage import is_keyring_available
from utils.logging import log

# Providers with a key dialog but no entry in PROVIDER_CHOICES (that list
# drives the ai_provider dropdown; Deepgram is transcription-only and
# selected via the streaming toggle instead — see providers/__init__.py).
_EXTRA_PROVIDER_NAMES = {"deepgram": "Deepgram"}


def _provider_display_name(provider: str) -> str:
    for name, provider_id in PROVIDER_CHOICES:
        if provider_id == provider:
            return name
    return _EXTRA_PROVIDER_NAMES.get(provider, provider)


# Placeholder hint per provider key format
_KEY_PLACEHOLDERS = {"openai": "sk-…", "gemini": "AIza…", "anthropic": "sk-ant-…"}

# Default dark-theme colours used when no colours dict is provided.
_DEFAULT_COLORS: dict[str, str] = {
    "app_bg": "#0b1020",
    "card": "#111827",
    "border": "#263449",
    "text": "#f8fafc",
    "muted": "#9ca3af",
    "accent": "#16a34a",
    "accent_hover": "#15803d",
    "button": "#1f2a44",
    "button_hover": "#263654",
    "entry": "#0f172a",
    "entry_border": "#334155",
    "panel_soft": "#182235",
}


def apply_dark_titlebar(
    win: tk.Misc, dark: bool | None = None, *, force_repaint: bool = False
) -> None:
    """Re-apply the themed DWM titlebar on a Windows toplevel.

    Sets the immersive dark-mode attribute directly (``iconbitmap()`` resets it
    to the light default). Shared by every themed CTkToplevel so the fix lives
    in one place.

    Pass ``dark`` to force light/dark explicitly (used on a runtime theme
    switch, where CTk's global appearance mode is stale); when omitted it
    follows CTk's current appearance mode.

    DWM only re-reads the attribute when the window is redrawn from hidden — a
    plain ``SetWindowPos(FRAMECHANGED)`` repaints the frame in place, which is
    enough for a freshly-mapped window (the icon-reset re-assert case) but
    leaves an already-shown window's caption stale. For a runtime switch on a
    window that stays open, pass ``force_repaint=True`` to cycle the map state
    SYNCHRONOUSLY (never via ``after()``, which can leave the window withdrawn
    for good — unlike CTk's own helper)."""
    if os.name != "nt":
        return
    try:
        import ctypes

        if dark is None:
            dark = ctk.get_appearance_mode().lower() == "dark"
        value = ctypes.c_int(1 if dark else 0)
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        # 20 = DWMWA_USE_IMMERSIVE_DARK_MODE (Win10 20H1+); 19 = pre-20H1.
        if (
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 20, ctypes.byref(value), ctypes.sizeof(value)
            )
            != 0
        ):
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 19, ctypes.byref(value), ctypes.sizeof(value)
            )
        if force_repaint and win.winfo_viewable():
            win.withdraw()
            win.update_idletasks()
            win.deiconify()
        else:
            # SWP_NOMOVE|NOSIZE|NOZORDER|FRAMECHANGED — repaint the title bar in
            # place, without moving, resizing, restacking, or hiding it.
            ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0027)
    except Exception:  # noqa: BLE001 — cosmetic only, must never crash a dialog
        pass


def show_message(
    root: tk.Misc,
    title: str,
    message: str,
    colors: dict[str, str] | None = None,
    *,
    confirm: bool = False,
    icon: str = "ℹ",
    icon_color: str | None = None,
    yes_label: str = "Yes",
    no_label: str = "No",
    ok_label: str = "OK",
) -> bool:
    """Show a themed CTk message/confirm dialog. Returns True if OK/Yes clicked.

    The height adapts to the message so long error strings aren't clipped."""
    c = colors or _DEFAULT_COLORS
    result = {"ok": False}

    w = 440
    dlg = ctk.CTkToplevel(root)
    dlg.title(title)
    dlg.geometry(f"{w}x200")
    dlg.resizable(False, False)
    dlg.configure(fg_color=c["app_bg"])
    dlg.transient(root)
    dlg.grab_set()

    def _set_icon() -> None:
        if sys.platform.startswith("win") and os.path.exists(ICON_PATH):
            try:
                dlg.iconbitmap(ICON_PATH)
            except Exception:
                pass
        elif os.path.exists(ICON_PATH_PNG):
            try:
                img = tk.PhotoImage(file=ICON_PATH_PNG)
                w, h = img.width(), img.height()
                factor = max(1, w // 64, h // 64)
                if factor > 1:
                    img = img.subsample(factor, factor)
                dlg.iconphoto(False, img)
            except Exception:
                pass
        apply_dark_titlebar(dlg)  # iconbitmap resets the titlebar → re-assert

    card = ctk.CTkFrame(
        dlg,
        fg_color=c["card"],
        border_color=c["border"],
        border_width=2,
        corner_radius=24,
    )
    card.pack(fill="both", expand=True, padx=16, pady=16)
    card.grid_columnconfigure(0, weight=1)
    card.grid_rowconfigure(0, weight=1)

    msg_row = ctk.CTkFrame(card, fg_color="transparent")
    msg_row.grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 8))
    msg_row.grid_columnconfigure(1, weight=1)

    _icon_color = icon_color or c.get("accent", "#16a34a")
    ctk.CTkLabel(
        msg_row,
        text=icon,
        font=ctk.CTkFont(size=20, weight="bold"),
        text_color=_icon_color,
        width=40,
        height=40,
        fg_color=c["panel_soft"],
        corner_radius=14,
    ).grid(row=0, column=0, padx=(0, 14), sticky="n")

    ctk.CTkLabel(
        msg_row,
        text=message,
        font=ctk.CTkFont(family="Segoe UI", size=14),
        text_color=c["text"],
        wraplength=w - 140,
        anchor="w",
        justify="left",
    ).grid(row=0, column=1, sticky="w")

    btn_row = ctk.CTkFrame(card, fg_color="transparent")
    btn_row.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 16))

    def _ok(_e=None) -> None:
        result["ok"] = True
        dlg.destroy()

    def _cancel(_e=None) -> None:
        dlg.destroy()

    if confirm:
        btn_row.grid_columnconfigure(0, weight=1, uniform="dlg_btns")
        btn_row.grid_columnconfigure(1, weight=1, uniform="dlg_btns")
        ctk.CTkButton(
            btn_row,
            text=yes_label,
            command=_ok,
            height=42,
            corner_radius=12,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=c.get("danger", "#dc2626"),
            hover_color=c.get("danger_hover", "#b91c1c"),
            text_color="#ffffff",
        ).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ctk.CTkButton(
            btn_row,
            text=no_label,
            command=_cancel,
            height=42,
            corner_radius=12,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=c["button"],
            hover_color=c["button_hover"],
            text_color=c["text"],
        ).grid(row=0, column=1, sticky="ew", padx=(6, 0))
        dlg.bind("<Return>", _ok)
        dlg.bind("<Escape>", _cancel)
    else:
        btn_row.grid_columnconfigure(0, weight=1)
        ctk.CTkButton(
            btn_row,
            text=ok_label,
            command=_ok,
            height=42,
            corner_radius=12,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=c["accent"],
            hover_color=c["accent_hover"],
            text_color="#ffffff",
        ).grid(row=0, column=0, sticky="ew")
        dlg.bind("<Return>", _ok)
        dlg.bind("<Escape>", _cancel)

    dlg.protocol("WM_DELETE_WINDOW", _cancel)

    # Size to content (long error strings would clip a fixed height), then
    # centre over the parent window.
    dlg.update_idletasks()
    h = max(180, min(520, card.winfo_reqheight() + 32))
    x = root.winfo_rootx() + (root.winfo_width() - w) // 2
    y = root.winfo_rooty() + (root.winfo_height() - h) // 2
    dlg.geometry(f"{w}x{h}+{x}+{y}")
    dlg.after(200, _set_icon)
    try:
        dlg.attributes("-topmost", True)
    except Exception:
        pass

    dlg.wait_window()
    return result["ok"]


# Backwards-compatible private alias for existing internal callers.
_ctk_msgbox = show_message


def prompt_for_api_key(
    root: tk.Tk,
    startup: bool,
    on_close: Callable[[], None],
    colors: dict[str, str] | None = None,
    texts: dict[str, str] | None = None,
    provider: str = "openai",
) -> str | None:
    """
    Prompt user for an API key.

    Args:
        root: The Tk root window.
        startup: Whether this is the first-run prompt.
        on_close: Callback to close the app if cancelled on startup.
        colors: Optional colour palette dict from the app; falls back to dark defaults.
        provider: Which AI provider the key belongs to.

    Returns:
        The entered API key, or None if cancelled/empty.
    """
    c = colors or _DEFAULT_COLORS
    t = texts or {}
    display = _provider_display_name(provider)
    if provider == "openai":
        prompt_text = (
            t.get("dlg_paste_key", "Paste your OpenAI API key")
            if startup
            else t.get("dlg_enter_key", "Enter a new OpenAI API key")
        )
    else:
        template = (
            t.get("dlg_paste_key_any", "Paste your {provider} API key")
            if startup
            else t.get("dlg_enter_key_any", "Enter a new {provider} API key")
        )
        prompt_text = template.format(provider=display)

    _keyring_missing = not is_keyring_available()

    dialog = ctk.CTkToplevel(root)
    dialog.title(f"{display} API Key")
    dialog.geometry(f"480x{'285' if _keyring_missing else '250'}")
    dialog.resizable(False, False)
    dialog.configure(fg_color=c["app_bg"])
    dialog.transient(root)
    dialog.grab_set()

    # Apply icon with a delay (CTkToplevel defers window creation)
    def _set_icon() -> None:
        if sys.platform.startswith("win") and os.path.exists(ICON_PATH):
            try:
                dialog.iconbitmap(ICON_PATH)
                return
            except Exception:
                pass
        if os.path.exists(ICON_PATH_PNG):
            try:
                img = tk.PhotoImage(file=ICON_PATH_PNG)
                w, h = img.width(), img.height()
                factor = max(1, w // 64, h // 64)
                if factor > 1:
                    img = img.subsample(factor, factor)
                dialog.iconphoto(False, img)
            except Exception:
                pass

    dialog.after(200, _set_icon)

    # Centre over parent window
    root.update_idletasks()
    _dlg_h = 285 if _keyring_missing else 250
    x = root.winfo_rootx() + (root.winfo_width() - 480) // 2
    y = root.winfo_rooty() + (root.winfo_height() - _dlg_h) // 2
    dialog.geometry(f"480x{_dlg_h}+{x}+{y}")

    try:
        dialog.attributes("-topmost", True)
    except Exception:
        pass

    # ── Card ──────────────────────────────────────────────────────────────
    card = ctk.CTkFrame(
        dialog,
        fg_color=c["card"],
        border_color=c["border"],
        border_width=2,
        corner_radius=24,
    )
    card.pack(fill="both", expand=True, padx=16, pady=(22, 16))
    card.grid_columnconfigure(0, weight=1)
    card.grid_rowconfigure(1, weight=1)  # push buttons to bottom

    # Centered header: badge + title
    header = ctk.CTkFrame(card, fg_color="transparent")
    header.grid(row=0, column=0, sticky="ew", pady=(18, 10))

    header_content = ctk.CTkFrame(header, fg_color="transparent")
    header_content.pack(anchor="center")

    sym = ctk.CTkLabel(
        header_content,
        text="⚿",
        font=ctk.CTkFont(family="Segoe UI Symbol", size=20, weight="bold"),
        text_color=c["accent"],
        width=44,
        height=44,
        fg_color=c["panel_soft"],
        corner_radius=16,
    )
    sym.grid(row=0, column=0, padx=(0, 12))

    title_lbl = ctk.CTkLabel(
        header_content,
        text=prompt_text,
        font=ctk.CTkFont(family="Segoe UI", size=16, weight="bold"),
        text_color=c["text"],
        anchor="w",
    )
    title_lbl.grid(row=0, column=1)

    # Entry row — no N/S sticky so it floats vertically centered in the expanded row
    entry_row = ctk.CTkFrame(card, fg_color="transparent")
    entry_row.grid(row=1, column=0, sticky="ew", padx=16, pady=10)
    entry_row.grid_columnconfigure(0, weight=1)

    entry = ctk.CTkEntry(
        entry_row,
        placeholder_text=_KEY_PLACEHOLDERS.get(provider, ""),
        show="●",
        height=46,
        corner_radius=14,
        border_width=2,
        font=ctk.CTkFont(family="Segoe UI", size=14),
        fg_color=c["entry"],
        border_color=c["entry_border"],
        text_color=c["text"],
        placeholder_text_color=c["muted"],
    )
    entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
    entry.focus_set()

    _visible = {"v": False}

    # Canvas-drawn eye icon — 20×20 canvas (small, crisp)
    _eye_frame = ctk.CTkFrame(
        entry_row,
        width=46,
        height=46,
        corner_radius=12,
        fg_color=c["button"],
        cursor="hand2",
    )
    _eye_frame.grid(row=0, column=1)
    _eye_frame.grid_propagate(False)
    _ic = tk.Canvas(
        _eye_frame,
        width=20,
        height=20,
        bg=c["button"],
        highlightthickness=0,
        bd=0,
        cursor="hand2",
    )
    _ic.place(relx=0.5, rely=0.5, anchor="center")

    def _draw_eye(hidden: bool) -> None:
        _ic.delete("all")
        col = c["text"]
        # Almond outline
        _ic.create_arc(
            1, 4, 19, 15, start=30, extent=120, style="arc", outline=col, width=1.5
        )
        _ic.create_arc(
            1, 4, 19, 15, start=210, extent=120, style="arc", outline=col, width=1.5
        )
        # Pupil
        _ic.create_oval(7, 7, 13, 13, fill=col, outline="")
        # Diagonal slash when hidden
        if hidden:
            _ic.create_line(2, 18, 18, 2, fill=col, width=1.5, capstyle="round")

    _draw_eye(True)

    def _toggle_visibility() -> None:
        _visible["v"] = not _visible["v"]
        entry.configure(show="" if _visible["v"] else "●")
        _draw_eye(not _visible["v"])

    def _on_eye_hover(entering: bool) -> None:
        bg = c["button_hover"] if entering else c["button"]
        _eye_frame.configure(fg_color=bg)
        _ic.configure(bg=bg)
        _draw_eye(not _visible["v"])

    for _w in (_eye_frame, _ic):
        _w.bind("<Button-1>", lambda _e: _toggle_visibility())
        _w.bind("<Enter>", lambda _e: _on_eye_hover(True))
        _w.bind("<Leave>", lambda _e: _on_eye_hover(False))

    result: dict[str, str | None] = {"key": None}

    # Keyring unavailable warning — only shown when secure storage is not available
    if _keyring_missing:
        warn_row = ctk.CTkFrame(card, fg_color="transparent")
        warn_row.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 4))
        warn_row.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            warn_row,
            text="⚠",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=c.get("warning", "#d97706"),
            width=20,
        ).grid(row=0, column=0, padx=(0, 6))
        ctk.CTkLabel(
            warn_row,
            text=t.get(
                "dlg_key_insecure_warning",
                "Keyring unavailable — key will be stored unencrypted.",
            ),
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=c.get("warning", "#d97706"),
            anchor="w",
            justify="left",
            wraplength=380,
        ).grid(row=0, column=1, sticky="ew")

    def on_ok() -> None:
        result["key"] = entry.get()
        dialog.destroy()

    def on_cancel() -> None:
        result["key"] = None
        dialog.destroy()

    # Buttons row — pinned to bottom via row weight
    btn_row = ctk.CTkFrame(card, fg_color="transparent")
    btn_row.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 14))
    btn_row.grid_columnconfigure(0, weight=1, uniform="api_btns")
    btn_row.grid_columnconfigure(1, weight=1, uniform="api_btns")

    ok_btn = ctk.CTkButton(
        btn_row,
        text=(texts or {}).get("dlg_ok", "OK"),
        command=on_ok,
        height=46,
        corner_radius=14,
        font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
        fg_color=c["accent"],
        hover_color=c["accent_hover"],
        text_color="#ffffff",
    )
    ok_btn.grid(row=0, column=0, sticky="ew", padx=(0, 6))

    cancel_btn = ctk.CTkButton(
        btn_row,
        text=(texts or {}).get("dlg_cancel", "Cancel"),
        command=on_cancel,
        height=46,
        corner_radius=14,
        font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
        fg_color=c["button"],
        hover_color=c["button_hover"],
        text_color=c["text"],
    )
    cancel_btn.grid(row=0, column=1, sticky="ew", padx=(6, 0))

    entry.bind("<Return>", lambda _e: on_ok())
    dialog.bind("<Escape>", lambda _e: on_cancel())
    dialog.protocol("WM_DELETE_WINDOW", on_cancel)

    dialog.wait_window()
    key = result["key"]

    if key is None:
        if startup:
            _ctk_msgbox(
                root,
                "API key required",
                "An API key is required to use this app.",
                c,
                icon="✕",
                icon_color=c.get("danger", "#dc2626"),
                ok_label=t.get("dlg_ok", "OK"),
            )
            root.after(100, on_close)
        return None

    key = key.strip()
    if not key:
        _ctk_msgbox(
            root,
            "Invalid key",
            t.get("dlg_key_empty", "Key cannot be empty."),
            c,
            icon="⚠",
            icon_color=c.get("warning", "#d97706"),
            ok_label=t.get("dlg_ok", "OK"),
        )
        if startup:
            root.after(
                100,
                lambda: prompt_for_api_key(
                    root,
                    startup=True,
                    on_close=on_close,
                    colors=colors,
                    texts=texts,
                    provider=provider,
                ),
            )
        return None

    # Validate API key format (OpenAI keys start with 'sk-')
    if provider == "openai" and not key.startswith("sk-"):
        _ctk_msgbox(
            root,
            "Invalid format",
            "OpenAI API keys typically start with 'sk-'. "
            "The key will be saved, but may not work.",
            c,
            icon="⚠",
            icon_color=c.get("warning", "#d97706"),
            ok_label=t.get("dlg_ok", "OK"),
        )
        log("API key format warning: key does not start with 'sk-'", level="WARNING")

    # Save and activate the key for its provider
    stored_securely = save_api_key(provider, key)
    log(f"API key saved ({provider}).", level="INFO")
    if stored_securely:
        _ctk_msgbox(
            root,
            "Saved",
            t.get("dlg_key_saved", "API key saved."),
            c,
            icon="✓",
            ok_label=t.get("dlg_ok", "OK"),
        )
    else:
        _ctk_msgbox(
            root,
            "Saved",
            t.get(
                "dlg_key_saved_insecure",
                "API key saved (stored in settings file, not keyring).",
            ),
            c,
            icon="⚠",
            icon_color=c.get("warning", "#d97706"),
            ok_label=t.get("dlg_ok", "OK"),
        )
    return key


def remove_api_key(
    is_running: bool,
    root: tk.Misc | None = None,
    colors: dict[str, str] | None = None,
    texts: dict[str, str] | None = None,
    provider: str = "openai",
) -> bool:
    """
    Remove the saved API key of a provider.

    Args:
        is_running: Whether the app is currently running (blocks removal).
        root: Parent window for dialogs.
        colors: App colour palette.
        provider: Which AI provider's key to remove.

    Returns:
        True if key was removed, False otherwise.
    """
    c = colors or _DEFAULT_COLORS
    t = texts or {}
    _root = root or tk._default_root  # type: ignore[attr-defined]

    if is_running:
        _ctk_msgbox(
            _root,
            "Stop first",
            t.get("dlg_stop_before_remove", "Stop the app before removing the key."),
            c,
            icon="⚠",
            icon_color=c.get("warning", "#d97706"),
            ok_label=t.get("dlg_ok", "OK"),
        )
        return False

    if not _ctk_msgbox(
        _root,
        "Remove key",
        t.get("dlg_remove_key_question", "Remove the saved API key?"),
        c,
        confirm=True,
        icon="⚠",
        icon_color=c.get("danger", "#dc2626"),
        yes_label=t.get("dlg_yes", "Yes"),
        no_label=t.get("dlg_no", "No"),
    ):
        return False

    clear_api_key(provider)
    log(f"API key removed ({provider}).", level="INFO")
    _ctk_msgbox(
        _root,
        "Removed",
        t.get("dlg_key_removed", "API key removed."),
        c,
        icon="✓",
        ok_label=t.get("dlg_ok", "OK"),
    )
    return True
