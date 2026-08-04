# gui_qt/ — Qt tree rules

The PySide6 migration (issue #44). Parallel to `gui/`; the two toolkits never share a
process. Project-wide context is in the root [AGENTS.md](../AGENTS.md).

## Running it

**PySide6 is not in the system Python** — it lives in `./venv`
(`PySide6-Essentials==6.11.1`, `requirements-qt.txt`). Use `./venv/Scripts/python.exe`
for anything Qt, pytest included.

```bash
./venv/Scripts/python.exe main.py --qt          # Tk stays the default without the flag
./venv/Scripts/python.exe -m pytest tests/test_gui_qt.py -q
```

## Hard rules

- **Nothing here may import a Tk-importing module.** The toolkit-free shared modules are
  `gui_qt/levels.py` and `gui_qt/subtitle_text.py`; `gui.control_state` and
  `gui.device_list` are the only cross-tree imports and both are toolkit-free. A test
  imports every Qt window in a subprocess and asserts no `tkinter`/`customtkinter` in
  `sys.modules`. `main.py` dispatches the already-running dialog on `_QT_MODE`.
- **Message boxes go through `gui_qt/dialogs.py`**, never `QMessageBox` — the system box
  is unthemed, hard-codes English buttons and plays the Windows alert sound (the icon
  triggers it). A test fails if one reappears.
- **Always-on-top goes through `widgets.set_window_on_top`**, never `setWindowFlag` —
  that recreates the native window (white flash; it used to make the overlay vanish).
  Read state with `is_window_on_top`; `QWidget.windowFlags()` is deliberately stale.
- **The overlay stays out of focus via `WA_ShowWithoutActivating`**, never
  `Qt.WindowDoesNotAcceptFocus` — the latter sets `WS_EX_NOACTIVATE`, which drops the
  window off the taskbar unless `WS_EX_APPWINDOW` is also forced, and the taskbar button
  is what OBS users need.
- **Window icons come from `gui_qt/icons.py`** — one cached `QIcon` built from the logo
  mark, shared by the QApplication, the panel and the wizard. Don't point a window at
  `MinbarLive.ico`: it carries the full wordmark in every size and smudges at 24–32 px.
  The `.ico` stays the fallback and stays what the EXE and shortcut use.
  `utils/icons.py` imports tkinter lazily — keep it that way.
- **Wheel gestures belong to the page, not to controls.** `Dropdown` and `Slider` both
  `event.ignore()` a wheel so the scroll area behind takes it. Don't drop a plain
  `QComboBox`/`QSlider` into the panel.
- **Every `QTextOption` needs `setTextDirection(Qt.LayoutDirectionAuto)`.** Its default
  is `LeftToRight`, *not* "work it out" — so an Arabic paragraph lays out LTR and its
  full stop, a neutral character, lands on the RIGHT. The words still run
  right-to-left inside the paragraph, which is why it looks almost correct.
  `QPainter.drawText` is already auto and needs nothing.
- **Font families come from `gui_qt/fonts.py`, per platform and per script.** Never
  hardcode one: `"Segoe UI"` for all three platforms is what made macOS build its alias
  table on every launch. Arabic gets its own stack because Qt reports the metrics of the
  family it was ASKED for and paints missing glyphs from a fallback family it was not —
  Arabic measured against a Latin descent overlapped the line below it.
- **Combo popups must not be the platform's.** `_ControlStyle.styleHint` answers
  `SH_ComboBox_Popup` and `SH_ComboBox_UseNativePopup` with 0, and `Dropdown` sets an
  explicit `QListView`. macOS otherwise opens a native NSMenu: unstyled, placed over the
  box, and ignoring `maxVisibleItems` so the list runs the height of the screen.

## Layout parity — do not "simplify" these back out

The tree reached parity with Tk deliberately; each item below was the point of a pass.

- **Main window:** the four-card grid, card icon badges, the collapsible Advanced card,
  the three stacked Display rows (font / height / backdrop opacity), the
  subtitle-appearance expander, the level meter, and the log panel that *shares* the
  window rather than widening it. `_COL2_MIN_W`/`_COL3_MIN_W` are measured from the
  columns' real minimums — don't lower them to the Tk numbers.
- **Onboarding:** step heading stays inside the card, the meter has no auto-stop, the
  Dark|Light control stays segmented.
- **Secondary windows:** history viewer, batch and the three popups (already-running,
  ffmpeg download offer, update banner). Don't reintroduce a second card in the batch
  window, the always-disabled "Show in history", or the appear-on-run progress bar.
- **Batch and announcement are hero + cards + a non-scrolling action bar.** Start and
  Send must stay reachable however tall the content gets; both cap at 92% of the screen.
  Don't move them into the scroll area or collapse the cards into one frame.

## Layout mechanics learned the hard way

- **Where the collapse lives depends on the column count.** 1–2 columns: the Advanced
  *card* collapses and "Weitere Einstellungen" is a plain section. 3 columns: the card is
  pinned open and the *group* collapses, closed by default. Three columns level through
  `_set_equal_column_heights`; two columns through `_level_two_column_bottoms` (pad the
  shorter column's last card, never by more than `_LEVEL_FILL_MAX_PX`). Don't merge them
  — they solve opposite problems.
- **A collapsed tail card is padded from ABOVE, never inflated** — in both paths.
- **List rows in the history viewer are a delegate, not item widgets.** The stylesheet's
  `QListWidget::item` padding leaves an index widget only the content rect (16 px of a
  40 px row), so both lines drew on top of each other. `RowDelegate` also elides at paint
  time and asks for width 0.
- **Measure a content-sized window with `layout().totalHeightForWidth()`, not
  `adjustSize()`** — a word-wrapped label's sizeHint reserves a line it doesn't use, and
  the surplus inflates whatever in the column can stretch.
- **A row added to a *visible* window's layout is hidden, and a hidden item contributes
  zero to the size hint.** Call `setVisible(True)` right after `addWidget` or the window
  measures itself without the rows it just gained. Delivering the posted `LayoutRequest`
  does not help — the item is skipped entirely. Pre-show hints don't settle either, so a
  content-sized window that gains rows re-measures in `showEvent`.

## Verifying changes

Launching the real app is expected. For headless checks, screenshot probes against a real
`ControlPanel` with a fake controller work well — with three traps that otherwise produce
confident, wrong passes:

1. **Stub `save_settings` in every module that imported it by name.** The import binds per
   module; patching `utils.settings` alone still lets a write through and clobbers the
   user's real `settings.json`.
2. **Pixel probes:** scale widget coordinates by `img.devicePixelRatio()`.
3. **Pointer probes:** use `QCursor.setPos` (logical), not `SetCursorPos` (physical), and
   assert `QApplication.widgetAt` really is the widget you meant.
4. **Stub `show_message` before driving any failure path.** `_finish_start`,
   `_finish_stop` and the batch worker report errors with a real modal dialog — a test
   that exercises those puts a box on the developer's desktop and blocks the run until
   it is dismissed (it looked like a 15-second test).

## The cut-over is gated

Nothing here has ever run on Linux or macOS, and issues #35/#39 — the migration's whole
justification — are Linux/macOS bugs. Do **not** delete `gui/`, drop
`customtkinter`/`arabic-reshaper`/`python-bidi`, or fold Qt into `MinbarLive.spec` first.
First two checks on the Ubuntu VM: does the frameless always-on-top overlay stack
correctly, and does the per-pixel-alpha backdrop render as a real dim rather than solid
black — exactly where Tk's integrated window mode failed (PR #25 r6).
