# gui/ — Qt tree rules

The app's only GUI, PySide6 (issue #44). It replaced a CustomTkinter tree of the same
name on 2026-08-04. Project-wide context is in the root [AGENTS.md](../AGENTS.md).

## Running it

**PySide6 lives in `./venv`, not in the system Python** (`PySide6-Essentials==6.11.1`,
now a plain `requirements.txt` entry). Use `./venv/Scripts/python.exe` for anything here,
pytest included.

```bash
./venv/Scripts/python.exe main.py
./venv/Scripts/python.exe -m pytest tests/test_gui.py -q
```

On Linux the tree asks for `QT_QPA_PLATFORM=xcb;wayland` (`gui/platform_setup.py`),
because a Wayland client can neither place its own windows nor stay on top — the two
things the overlay is made of. Every launch logs which plugin actually loaded; a bug
report about overlay position or stacking starts with that line.

**A Linux machine running this from source needs the xcb libraries** — the set is in
the README and in both workflows. `libxcb-cursor0` is the one Qt names in its hint, and
it is NOT the whole list: that hint is printed whenever the plugin fails to load, for any
reason, and the real answer comes from
`find .venv -name libqxcb.so -exec ldd {} \; | grep 'not found'`. Without them the plugin
is found and refuses to load, and the list above falls through to Wayland. Then the overlay is centred by
the compositor, always-on-top does nothing, and lowering the height walks the footer UP
instead of bringing the top edge down, because a window that cannot be moved shrinks from
its fixed top-left. All three read as application bugs and none of them is reachable from
application code, so the app also says it at startup, in the log and on stderr. See the
packaging section below for the shipped build.

## Hard rules

- **Nothing here may import a Tk-importing module** — and as of 2026-08-05 there is no
  such module left anywhere in the repo, test probes aside. A test imports every window in
  a subprocess and asserts no `tkinter`/`customtkinter` in `sys.modules`; `MinbarLive.spec`
  excludes `tkinter` outright so a stray import cannot pull Tcl/Tk back into the bundle.
  `gui/control_state.py`, `gui/device_list.py`, `gui/levels.py`, `gui/palette.py`,
  `gui/i18n.py` and `gui/subtitle_text.py` are the toolkit-free modules — keep them that
  way, they are what makes the rules testable headlessly.
- **Message boxes go through `gui/dialogs.py`**, never `QMessageBox` — the system box
  is unthemed, hard-codes English buttons and plays the Windows alert sound (the icon
  triggers it). A test fails if one reappears.
- **Always-on-top goes through `widgets.set_window_on_top`**, never `setWindowFlag` —
  that recreates the native window (white flash; it used to make the overlay vanish).
  Read state with `is_window_on_top`; `QWidget.windowFlags()` is deliberately stale.
  On X11 the cheap path does not exist: the flag is the `_NET_WM_STATE_ABOVE` property
  and Qt's xcb plugin only writes it while the window is unmapped, so `set_window_on_top`
  re-creates and re-shows there. That is why the setting did nothing at all on Linux.
- **The overlay stays out of focus via `WA_ShowWithoutActivating`**, never
  `Qt.WindowDoesNotAcceptFocus` — the latter sets `WS_EX_NOACTIVATE`, which drops the
  window off the taskbar unless `WS_EX_APPWINDOW` is also forced, and the taskbar button
  is what OBS users need.
- **Window icons come from `gui/icons.py`** — one cached `QIcon` built from the logo
  mark, shared by the QApplication, the panel and the wizard. Don't point a window at
  `MinbarLive.ico`: it carries the full wordmark in every size and smudges at 24–32 px.
  The `.ico` stays the fallback and stays what the EXE and shortcut use.
  `utils/icons.py` is toolkit-free (PIL only) — keep it that way.
- **Wheel gestures belong to the page, not to controls.** `Dropdown` and `Slider` both
  `event.ignore()` a wheel so the scroll area behind takes it. Don't drop a plain
  `QComboBox`/`QSlider` into the panel.
- **Every `QTextOption` needs `setTextDirection(Qt.LayoutDirectionAuto)`.** Its default
  is `LeftToRight`, *not* "work it out" — so an Arabic paragraph lays out LTR and its
  full stop, a neutral character, lands on the RIGHT. The words still run
  right-to-left inside the paragraph, which is why it looks almost correct.
  `QPainter.drawText` is already auto and needs nothing.
- **Font families come from `gui/fonts.py`, per platform and per script.** Never
  hardcode one: `"Segoe UI"` for all three platforms is what made macOS build its alias
  table on every launch. Arabic gets its own stack because Qt reports the metrics of the
  family it was ASKED for and paints missing glyphs from a fallback family it was not —
  Arabic measured against a Latin descent overlapped the line below it.
- **Combo popups must not be the platform's** — and that takes two overrides, not one.
  `_ControlStyle.styleHint` answers `SH_ComboBox_Popup` and `SH_ComboBox_UseNativePopup`
  with 0, and `Dropdown` sets an explicit `QListView`; macOS otherwise opens a native
  NSMenu: unstyled, placed over the box, and ignoring `maxVisibleItems` so the list runs
  the height of the screen. The list's RECTANGLE is a separate question:
  `_ControlStyle.subControlRect` answers `option.rect` for `SC_ComboBoxListBoxPopup`
  because `QMacStyle` returns one placed OVER the box (so the current item sits under the
  pointer), which drew the open list's first row on top of the closed box's own text.
- **Font families in the stylesheet come from `gui/fonts.py` too.** Qt's stylesheet
  parser does not implement the CSS generic families: `sans-serif` and `monospace` are
  family NAMES to it. Asking for one that does not exist makes Qt populate its whole alias
  table and print "Populating font family aliases took N ms. Replace uses of missing font
  family …" — which is what a hardcoded `sans-serif` tail and `"Consolas", "Menlo"` did on
  macOS. A test walks every `font-family` in the sheet; keep it passing rather than
  chasing the warning, which only ever names the first miss.
- **The macOS overlay is laid out inside the work area, always.** Nothing a Qt client can
  ask for puts a window above the Dock or the menu bar — a stays-on-top window floats
  above other applications and still below both — so a full-height overlay loses its
  bottom strip and the disclaimer pill with it. `_MACOS` in `gui/subtitle_window.py`.
  Windows and X11 keep the whole monitor when topmost; OBS captures the full frame
  because of it.

## Layout parity — do not "simplify" these back out

The tree reached parity with the CustomTkinter panel deliberately; each item below was
the point of a pass. References to "the Tk overlay" and to functions like
`_stack_overlap` or `_fit_geometry_to_monitor` are to code that was deleted in the
cut-over — read it at `git show 8a0440c:gui/subtitle_window.py`, the last commit that
still had it.

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

- **The panel's minimum WIDTH is measured, never a constant.** The card area
  scrolls vertically only, so below the cards' own minimum the content is cut off
  with no horizontal bar to reach it, and the vertical bar then draws on top of
  the clipped edge. Two traps, both of which cost a round: measure it from
  `cards_host.minimumSizeHint()` and at three columns you get the three minimums
  ADDED UP, which pins a wide window open — use `CardGrid.minimum_width()`, the
  widest single column, which is what the reflow can actually collapse to. And
  measure it before the first show and you get 50 px, because a card's padding,
  border and font all come from the stylesheet and Qt applies that at polish time.
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
- **Subtitle lines are placed by their BASELINE, not by the top of their box.** A line's
  ascent is the tallest of the font engines that actually drew it, and a single glyph
  borrowed from a fallback family (the ﷺ/ﷻ honorifics on Linux) makes it far taller than
  the metrics every other figure in `_layout_text` comes from. Placing the box top on our
  own rhythm then drops the line by the difference — the blank band that opens above it is
  the honorific appearing to push its paragraph away. The correction only ever pulls a
  line up, and is measured against `QFontMetricsF` because `QFontMetrics` rounds and
  `QTextLine` does not.
- **`setGeometry` is a request.** An X11 WM may grant the size and refuse the position
  (GNOME keeps a frameless window clear of its struts), and the overlay's bottom strip —
  the disclaimer pill — then hangs below the screen. `_fit_to_screen` reads back what was
  granted 250 ms later and shrinks into it, as the Tk overlay does
  (`_fit_geometry_to_monitor`). It cannot help under Wayland: there the position Qt
  reports is the one that was asked for.
- **A row added to a *visible* window's layout is hidden, and a hidden item contributes
  zero to the size hint.** Call `setVisible(True)` right after `addWidget` or the window
  measures itself without the rows it just gained. Delivering the posted `LayoutRequest`
  does not help — the item is skipped entirely. Pre-show hints don't settle either, so a
  content-sized window that gains rows re-measures in `showEvent`.

## Verifying changes

Launching the real app is expected. For headless checks, screenshot probes against a real
`ControlPanel` with a fake controller work well — with five traps that otherwise produce
confident, wrong passes:

1. **Stub `save_settings` in every module that imported it by name.** The import binds per
   module; patching `utils.settings` alone still lets a write through and clobbers the
   user's real `settings.json`.
2. **Pixel probes:** scale widget coordinates by `img.devicePixelRatio()`.
3. **Pointer probes:** use `QCursor.setPos` (logical), not `SetCursorPos` (physical), and
   assert `QApplication.widgetAt` really is the widget you meant.
4. **Keep a `QTextLayout` referenced while you read its lines.** `QTextLine` borrows
   from the layout, so `layout_returning_call()[0].lineAt(0)` leaves the line pointing at
   freed memory — the process dies with a heap-corruption exception, not an exception you
   can catch. Bind the layout to a local first.
5. **Stub `show_message` before driving any failure path.** `_finish_start`,
   `_finish_stop` and the batch worker report errors with a real modal dialog — a test
   that exercises those puts a box on the developer's desktop and blocks the run until
   it is dismissed (it looked like a 15-second test).
6. **Stub `ensure_keys` before driving any Start path**, for the same reason and one
   worse property: whether it opens a dialog depends on the machine. `_on_start` (batch)
   and the panel's Start ask for any missing provider key first, and `ensure_keys` opens
   a real modal `ApiKeyDialog` when none is stored. With keys in your keychain it returns
   instantly and the test passes; on a runner with an empty keychain it blocks — this took
   CI's Windows job down with an access violation and a 20-minute cancellation, on tests
   that had been green locally for weeks. Patch it on the module that *imported* it
   (`gui.batch_window.ensure_keys`), never on `gui.api_keys` — the import binds per module.

**A green local run is not evidence for anything that reads the keychain, the filesystem
or the display.** Reproduce a runner by patching `has_usable_key` to False before
believing a Start path is covered.

## Packaging requirement for Phase 7

**The Linux builder must install the xcb libraries before `pyinstaller` runs**, or the
AppImage ships without them. Since Qt 6.5 the xcb platform plugin has a hard dependency
on **`libxcb-cursor0`**, which is *not* installed by default on a stock Ubuntu/Debian
GNOME desktop — a plain user's AppImage would find the plugin, fail to load it, and fall
through to Wayland, where the overlay cannot be positioned or kept on top. Never solve
this by telling users to install a package.

PyInstaller bundles the shared libraries it finds the collected binaries linking against
ON THE BUILD MACHINE — the same reason `release.yml` already installs `libportaudio2`.
Both workflows now install the xcb set alongside it. The xvfb smoke launch is the guard:
a Qt app whose only platform plugin will not load dies at once, and CI requires it to stay
up for 30 s.

**Two smoke launches, and only the second one proves anything about this.** The first
runs on the build machine, which installed the xcb set so PyInstaller could find it — it
passes whether or not those libraries reached the AppImage. The second runs the same file
in a bare `ubuntu:24.04` container that has none of them. A Qt app whose xcb plugin will
not load falls through to the Wayland plugin, which under xvfb is no plugin at all, so it
dies at once instead of reaching the 30 s timeout. Keep the container's package list
minimal and keep the Qt xcb set out of it; that list IS the test.

## The cut-over happened

`gui/` IS the Qt tree now (2026-08-04). The CustomTkinter tree, `customtkinter`,
`arabic-reshaper` and `python-bidi` are gone, `main.py` starts Qt with no flag, and the
spec excludes `tkinter` outright so a stray import can never pull Tcl/Tk back into the
bundle. `gui/control_state.py` and `gui/device_list.py` survived the deletion because they
are toolkit-free and this tree imports them.

The leftovers went on 2026-08-05: `utils/api_key_manager.py` (the Tk key dialog
`gui/api_keys.py` replaced — 820 lines importing a `customtkinter` that is no longer in
`requirements.txt`, so a fresh checkout could not import it at all; only a pre-migration
venv still has the package) and with it `utils/icons.py`'s
`scaled_icon_photo`/`logo_photo`, whose only caller it was. `utils/icons.py` is now
toolkit-free and there is no `import tkinter` left in non-test code.

`utils/windows_dpi.py` is still here and still unused at runtime — see `gui/app.py` for
why calling it would be a bug. Its tests are the record of the contract, and
`test_windows_dpi.py`'s coordinate probe is the one remaining place that builds a Tk
window (a Windows-only subprocess).
