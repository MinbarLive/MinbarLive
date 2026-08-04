"""Theme palettes — the single source of truth for every colour in the app.

Imports no GUI toolkit: these are plain hex strings. ``gui/theme.py`` builds the
stylesheet from them and ``gui/subtitle_window.py`` paints from them, so a
colour is defined once and never re-typed into a widget.
"""

LIGHT: dict[str, str] = {
    "app_bg": "#edf2f7",
    "sidebar": "#f8fafc",
    "card": "#ffffff",
    "panel": "#ffffff",
    "panel_soft": "#f1f5f9",
    "border": "#d7dee8",
    "shadow": "#cbd5e1",
    "text": "#111827",
    "muted": "#64748b",
    "log_bg": "#fbfdff",
    "log_text": "#172033",
    "accent": "#15803d",
    "accent_hover": "#166534",
    "accent_soft": "#dcfce7",
    "danger": "#dc2626",
    "danger_hover": "#b91c1c",
    "danger_soft": "#fee2e2",
    "warning": "#d97706",
    "button": "#e2e8f0",
    "button_hover": "#cbd5e1",
    "entry": "#f8fafc",
    "entry_border": "#cbd5e1",
}

DARK: dict[str, str] = {
    "app_bg": "#0b1020",
    "sidebar": "#0f172a",
    "card": "#111827",
    "panel": "#111827",
    "panel_soft": "#182235",
    "border": "#263449",
    "shadow": "#050817",
    "text": "#f8fafc",
    "muted": "#9ca3af",
    "log_bg": "#0a0f1d",
    "log_text": "#d8e3f0",
    "accent": "#16a34a",
    "accent_hover": "#15803d",
    "accent_soft": "#163821",
    "danger": "#dc2626",
    "danger_hover": "#b91c1c",
    "danger_soft": "#421719",
    "warning": "#f59e0b",
    "button": "#1f2a44",
    "button_hover": "#263654",
    "entry": "#0f172a",
    "entry_border": "#334155",
}


def palette(theme_mode: str) -> dict[str, str]:
    """Return the colour map for ``theme_mode``; anything but "light" is dark."""
    return dict(LIGHT) if theme_mode == "light" else dict(DARK)
