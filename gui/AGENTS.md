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
- **The debug log is English, never translated** (2026-08-07). Every `log()` call is a
  plain f-string. It is not an audience-facing surface: the panel hides it by default, it
  is the operator's diagnostic view, and the log FILE is what gets pasted into a GitHub
  issue — a Turkish stack of breadcrumbs makes triage worse, not better. Three keys
  (`log_started`, `log_stopped`, `log_gui_language_changed`) were the last translated
  lines against 19 English ones; they were de-translated and their keys deleted rather
  than the other 19 translated. A test asserts no `log_*` key comes back. This does NOT
  weaken the localisation invariant in the root `AGENTS.md`, which is about the *subtitle
  window* — that stays localized to the target language.
- **Message boxes go through `gui/dialogs.py`**, never `QMessageBox` — the system box
  is unthemed, hard-codes English buttons and plays the Windows alert sound (the icon
  triggers it). A test fails if one reappears.
  - **A confirm that deletes something passes `destructive=True`** and names its two
    actions with `yes_text`/`no_text`. `#accent` is the app's green "go", so without
    the flag the accepting button marks *delete everything* as the recommended half
    of the choice; with it the button wears the same warning red as every other
    destructive control. Pair it with `default_yes=False` so Return cannot press it.
- **Both notices above the cards are `gui/notice_banner.py`.** The update offer and
  the review prompt share the class *and* the object names (`update_banner`,
  `update_text`, `banner_skip`, `banner_close`), so the sheet has one set of rules.
  Subclass it rather than building a third bar: it already carries the two Qt rules
  that are easy to get wrong — **hidden at construction, after being parented** (a
  parentless widget made visible is a top-level window), and **outer spacing as a
  stylesheet margin, not a layout's**, so a hidden banner takes no room instead of
  leaving a gap above the cards.
- **Always-on-top goes through `widgets.set_window_on_top`**, never `setWindowFlag` —
  that recreates the native window (white flash; it used to make the overlay vanish).
  Read state with `is_window_on_top`; `QWidget.windowFlags()` is deliberately stale.
  On X11 the cheap path does not exist: the flag is the `_NET_WM_STATE_ABOVE` property
  and Qt's xcb plugin only writes it while the window is unmapped, so `set_window_on_top`
  re-creates and re-shows there. That is why the setting did nothing at all on Linux.
- **Never clear a window flag with `~` on the enum.** `~Qt.WindowStaysOnTopHint` is
  `0x01fbffff` — PySide6 complements within the enum's *declared range*, so `flags & ~x`
  silently drops every window flag above it, `WindowCloseButtonHint` (0x08000000) first.
  A window without that hint keeps its ✕ and Windows draws it **greyed out and inert**,
  on a focused window, for the rest of the process. That is the "the X is sometimes
  greyed out" report, twice; it was never random — `always_on_top_mode` defaults to
  *When running*, so the first Stop of every session did it. Use plain ints:
  `Qt.WindowType(int(flags) & ~int(flag))`, as `widgets._with_on_top` does.
- **Always-on-top is a band, not a rank, and the app has several windows in it.**
  Inside the band the order is whoever was raised last, so `subtitle_window._keep_on_top`
  — the once-a-second restack that keeps the overlay above a clicked taskbar — buried
  the control panel with it: the panel came forward when clicked and sank again within
  the second. The fix is a standing position for the overlay, **not** repeatedly lifting
  the panel: `widgets.place_window_behind` puts the overlay directly under the window
  passed as `stay_under`, so the panel is never restacked at all. A window that keeps
  forcing itself forward is its own defect — the maintainer rejected that shape
  explicitly. It falls back to `raise_()` off Windows, with no panel, or when the panel
  is minimized or not itself topmost (each of those would strand the overlay at the
  bottom of the stack). **Known cost:** a click on the taskbar puts the shell above both
  windows, and the overlay only comes back over it when the panel next does — clicking
  the panel fixes it. Beating the taskbar without moving the panel is not possible.
- **The native title bar goes through `widgets.set_titlebar_dark`**, never the
  stylesheet — a caption bar is the window manager's, not a widget. On Windows it
  follows the SYSTEM light/dark preference and nothing the app asks for, so a
  light panel under a dark Windows kept a black bar joined to a white header.
  `DWMWA_USE_IMMERSIVE_DARK_MODE` is the only lever and Qt exposes no API for it,
  which is why that one helper is ctypes. `theme.apply_titlebar_theme` sweeps the
  windows that already exist (a theme switch); a window built *after* `apply_theme`
  re-applies it in its own `showEvent` — the control panel and the wizard both do.
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
  columns' real minimums — don't lower them to the Tk numbers. `_COL2_MIN_W` is only
  the **floor**: `two_column_min_width()` raises it to what the columns in front of it
  measure, because that figure comes from the font engine (658 px in Arabic, 758 in
  German, 869 on Linux) and no single constant is right everywhere. Leave `_COL3_MIN_W`
  a constant — column C's minimum moves with the arrangement, so measuring it would
  oscillate.
- **A window floor is always clamped to the screen.** `setMinimumSize` is honoured
  whatever the display can show, so the excess hangs off the edge and cannot be dragged
  back. Any new arrangement with a floor of its own gets the same clamp.
- **The sidebar beside the log is a FIXED width, never a min/max range.** The log
  stretches and the sidebar does not, so a sidebar given a range gets its floor and the
  log takes every spare pixel — it stops honouring `_SIDEBAR_W_WITH_LOG` on *every*
  screen. `_log_share()` picks the number to fix it at, measured against the **screen**
  rather than the current width so a resize cannot feed back into itself.
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
- **`_available_width()` reserves the vertical scroll bar whether or not it is up, and that
  is load-bearing.** The viewport's width depends on the bar; the column count decides how
  tall the content is; the content height decides whether the bar shows. Feeding the live
  viewport width back into the column decision closes that loop at any window width within
  the bar's width of a threshold — three columns pin the Advanced card open, the taller
  content summons the bar, the bar drops the viewport back under `_COL3_MIN_W`, two columns
  let the content shrink, the bar goes. Measured: widths **1030–1039 oscillated 3/2/3/2
  forever** at 720 and 800 px tall. Reserving it unconditionally breaks the loop and matches
  `_cards_minimum_width()`, which already assumes the bar is there. It moves the effective
  thresholds by the bar's width — that is the price, not a bug. **Don't "use the real
  viewport width" here.**
- **`setMinimumSize` RESIZES a window that is under the new floor** — synchronously, on the
  spot. So raising a minimum is a layout change *and* a resize, and any code that wants the
  size a widget had before must read it **before** the call, not after. This is what widens
  the control panel when the log opens (`_apply_log_panel_widths` → `_apply_minimum_size`):
  measured on a shown panel, 702 px becomes 840 px with no `resize()` executed anywhere.
  The explicit `resize()` in `_toggle_log_panel` is dead on a shown window and only carries
  the hidden case.
- **The `panel` fixture in `tests/test_gui.py` never calls `show()`**, and
  `_apply_minimum_size` early-returns a minimum of **zero** while a window is invisible. So
  anything driven by the window's minimum is simply *absent* under that fixture — a test
  for it passes against code that does nothing. Show the window in tests that touch window
  sizing. (The rule "widget geometry is meaningless on a window that was never shown"
  applies to the MINIMUM too, not only to size hints — that gap shipped a fix that passed
  its own tests and did not work in the app.)
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
- **The height slider means two different things, and each has its OWN stored field**
  (`window_height_percent` 5–100, `static_lift_percent` 0–50). Everywhere except
  transparent static it is a HEIGHT: the overlay is a band taking that share of the
  screen. In **transparent static** the overlay has no backdrop of its own, so it
  takes the whole monitor (`_effective_height_percent`) and the slider becomes a LIFT
  (`_static_lift`) — how far the subtitles and the footer pill *together* sit above
  the bottom edge. Both are offset by the one figure or the disclaimer drifts away
  from its text.
  - **Two fields since 2026-08-07; do not merge them back.** One carried both, on the
    argument that the panel read it clamped and wrote only on a real drag. That
    protects the number, not the setting: the ranges do not share a floor, so every
    toggle of Transparent handed the other meaning a value off the wrong scale — a
    lift of 0 came back as a 0%-tall band (a one-pixel overlay, subtitles gone) and
    the loader's floor of 5 rewrote that lift on every restart.
  - `set_static_lift_percent` must NOT re-place the window. The overlay is already
    the whole monitor there; only what is painted inside it moves.
  - The row **never greys out**; its range, VALUE, readout and caption swap instead.
    Block the slider's signals around the swap — `setRange` clamps and `setValue`
    moves, and **both** emit `valueChanged`, so arriving in a mode would write its
    value into the field of the mode just left.
  - Entering or leaving transparent static re-places the window, because the
    window's height changes although the setting did not.
- **Static never scrolls, so in a band the text is FITTED to it**
  (`_static_fit_scale`), as the Tk overlay did (`_static_fonts_for_content`).
  A rescue, not a policy: if the block already fits, the configured size is used
  untouched. Otherwise the answer is the **largest** size that fits, by bisection —
  search in both directions, because wrapping moves in whole words so the linear
  estimate undershoots and a shrink-only search left 49 px of text in a 66 px band.
- **`reserved_bottom` is capped at half a short overlay.** The pills are a fixed
  size and deliberately do not scale with the subtitle font, so on a thin band they
  asked for more room than the window had: the content area collapsed to one pixel
  and the pill was laid out from a bottom edge above its own top.
- **`reserved_bottom` holds back what a block DRAWS, not the height it measures.** In
  transparent static a card extends `_CARD_PAD_Y` below its own text (`_ribbon_rects`),
  the same 8 px as `PILL_CLEARANCE` — so reserving for the text alone put the card's
  bottom border exactly on the disclaimer. The mode adds the pad to the reserve. Any
  future decoration drawn outside a block's measured height belongs there too.
- **The side-by-side panels keep the SAME clearance above them as below** (`PILL_CLEARANCE`
  at both ends, `_column_panel_rects`). They are the backdrop in this layout — the window
  paints none — so where they start IS the top of the overlay, and at the feed's own
  `FEED_TOP_RATIO` inset a band of live video stood above them while a hairline stood
  below: at 100% height the overlay visibly stopped short of the monitor's upper border.
  Maintainer's choice of the three options offered, 2026-08-07. The first line follows the
  panel and sits at its own `COLUMN_PANEL_PAD_Y` inside it (`_feed_top`) — don't put it
  back on a share of the window height, which is what the other layouts still use.
- **The transparent-static backdrop is one box per RENDERED LINE, tiled**
  (`_ribbon_rects`). Per line, because a paragraph's width is its longest line, so
  one box round a wrapped sentence is a rectangle with ragged text inside it. Tiled
  — each box ending exactly where the next begins — because a block's source and
  its translation are pulled together until their metric BOXES overlap (`_pair_gap`
  goes negative on purpose; only the ink is held apart), so two independent
  backdrops drew one on top of the other and the translation's hid the source's
  last line. Draw every backdrop before any text for the same reason: interleaved,
  a backdrop still lands on the line above however well the rects tile. Boxes
  overlap by their own corner radius so the rounding does not notch each join, and
  are filled as ONE `Qt.WindingFill` path — odd-even would punch the overlap out as
  a hole, and filling them separately composites the translucent black twice.
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
