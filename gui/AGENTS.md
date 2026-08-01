# gui/ — CustomTkinter tree rules

The shipped GUI. Being migrated to `gui_qt/` (issue #44) but **still the default** —
`main.py` without `--qt` lands here. Project-wide context is in the root
[AGENTS.md](../AGENTS.md).

## Structure

`AppGUI` in `app_gui.py` is composed from four mixins; their methods run on the `AppGUI`
instance and share its attributes (`_colors`, `gui_texts`, `_saved_settings`, the
themed-widget registries).

| Module | Mixin | Owns |
| --- | --- | --- |
| `app_gui.py` | — | Layout, cards, start/stop, pipeline queue polling, theme/language application |
| `widgets.py` | `WidgetFactoryMixin` | Theme palette, themed dialogs (`_alert`/`_confirm`), icon/titlebar helpers, widget factory |
| `history_view.py` | `HistoryViewMixin` | History \| Batch \| Log viewer + Summarise-session dialog |
| `batch_view.py` | `BatchViewMixin` | Batch/File window + worker/progress plumbing (state lives on `AppGUI` and survives window close) |
| `settings_view.py` | `SettingsViewMixin` | Settings window + per-provider API-key management |

Other modules: `control_state.py` (toolkit-free Settings logic), `dropdown.py`
(`CustomDropdown`, shared with onboarding), `onboarding.py` (first-run wizard),
`device_list.py` (audio device enumeration), `subtitle_window.py` (the overlay).

**New secondary windows belong in their own mixin module, not in `app_gui.py`.** Mixins
were chosen over standalone window classes deliberately — it allowed verbatim moves with
no `self.app.X` rewrites through untested rendering code. Don't convert them to classes
without a user decision.

## Subtitle window

`subtitle_window.py` has three display modes: **Realtime** (top-down feed with the
in-progress transcript line, streaming-only, the default), **continuous scroll**, and
**static**. Any change must account for multi-monitor placement and the transparent
overlay option.

## Tests — two layers

- **`tests/test_control_state.py`** covers the panel's Settings-derived rules from
  `gui/control_state.py` (required keys, subtitle-mode choices, strategy application,
  provider repair) headlessly in under a second. **No Tk import is allowed in that
  module**, and a test enforces it.
- **`tests/test_app_gui.py`** builds a real `AppGUI` (fake controller, patched
  `load_settings`/`save_settings`/keyring, `hide_subtitle_on_stop=True`,
  `check_for_updates=False`) for what genuinely needs a window.

**New control-panel logic that only touches `Settings` belongs in `control_state.py`, not
as an `AppGUI` method** — every real-window test costs a Tk root and a display.

Two fixture traps:

1. **The fixture must not call `destroy()` after `on_close()`** — `on_close()` already
   quits and destroys. Doing both corrupts the Tcl interpreter and later roots fail with
   `invalid command name "tcl_findLibrary"`.
2. **`resolve_provider_by_keys` must be patched**, or results depend on whichever keys are
   stored on the dev machine.

Tests drive handlers, not pixels — visual changes still need a real run.

## Note

CustomTkinter has a long tail of rendering, theming and window-lifecycle traps
(post-map painting, DWM titlebar repaint on theme switch, ScalingTracker leaks across
roots, layered-window alpha, canvas bbox vs. glyph ink). Those are recorded in the
maintainer's local memory rather than here; ask before assuming a layout approach works.
