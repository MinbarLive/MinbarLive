"""Subtitle overlay (Qt) — the audience-facing window.

Port of ``gui/subtitle_window.py``. The behaviour is intended to match; what
changes is how much machinery it takes, and three whole classes of bug that
stop being possible:

* **No text shaping layer.** ``_reshape_rtl`` and its ``_TK_HANDLES_ARABIC`` /
  ``_TK_SHAPES_ARABIC`` platform branches are gone: logical text goes straight
  to Qt, which shapes and bidi-orders it with HarfBuzz everywhere.
* **No manual line wrapping.** The Tk version wrapped text itself, and wrapping
  *shaped* text is what put the end of an RTL sentence on the first line. Qt
  wraps during layout, after shaping, so the bug has no place to live.
* **No ink measurement.** ``_stack_overlap`` existed because a Tk canvas text
  bbox reports font metrics rather than glyph ink, and the fix had to be
  re-derived per font family (it broke on Linux, where leading differs). Qt's
  ``QFontMetrics.lineSpacing()`` is a real baseline rhythm — which is exactly
  the model that pass concluded was correct.

Transparency is genuine per-pixel alpha, not the ``-transparentcolor`` green
chroma key, so anti-aliased glyph edges no longer fringe over video and no
colour is forbidden in subtitle text.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import QRect, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QPainter,
    QPainterPath,
)
from PySide6.QtWidgets import QWidget

from config import (
    REALTIME_BLOCK_SPACING,
    REALTIME_LIVE_MAX_ROWS,
    REALTIME_MAX_BLOCK_CHARS,
)
from gui_qt.fonts import source_font, subtitle_font
from gui_qt.palette import palette
from utils.settings import (
    SUBTITLE_MODE_CONTINUOUS,
    SUBTITLE_MODE_REALTIME,
    SUBTITLE_MODE_STATIC,
)

# Gap between the source line and its translation inside one block. They read as
# one utterance, so this is much tighter than the gap between blocks.
PAIR_GAP = 6
# Distance from the window's bottom edge to the footer pill, and between pills.
FOOTER_MARGIN = 18
PILL_GAP = 12
# Side margin as a fraction of window width, so a line never runs edge to edge.
SIDE_MARGIN_RATIO = 0.06
# Continuous mode advances by this many pixels per frame at speed 1.0.
SCROLL_PIXELS_PER_FRAME = 1.0
FRAME_MS = 16


@dataclass
class Block:
    """One settled utterance: a translation, optionally with its original."""

    translation: str
    source: str | None = None
    # Filled in by the layout pass; pixel height of the whole block.
    height: int = field(default=0, repr=False)


class SubtitleWindow(QWidget):
    """Frameless, always-on-top, per-pixel-alpha subtitle overlay."""

    def __init__(
        self,
        on_close=None,
        monitor_index: int = 1,
        font_size_base: int = 40,
        source_font_size_base: float = 40 / 0.7,
        translation_text_color: str = "",
        source_text_color: str = "",
        target_language: str = "German",
        subtitle_mode: str = SUBTITLE_MODE_STATIC,
        scroll_speed: float = 1.0,
        transparent_static: bool = False,
        window_height_percent: int = 100,
        show_footer: bool = True,
        theme_mode: str = "dark",
        bilingual_mode: bool = False,
        always_on_top: bool = True,
        adaptive_catchup: bool = False,
        on_stop=None,
    ):
        super().__init__()
        self._on_close = on_close
        self._on_stop = on_stop
        self._monitor_index = monitor_index
        self._target_language = target_language
        self._mode = subtitle_mode
        self._scroll_speed = scroll_speed
        self._transparent_static = transparent_static
        self._height_percent = window_height_percent
        self._show_footer = show_footer
        self._theme_mode = theme_mode
        self._bilingual = bilingual_mode
        self._font_size_base = font_size_base
        self._source_font_size_base = source_font_size_base
        self._translation_color = translation_text_color
        self._source_color = source_text_color

        self._blocks: list[Block] = []
        self._live_text: str | None = None
        self._live_settled = False
        self._announcement: str | None = None
        self._stopped_hint = False
        self._scroll_offset = 0.0
        self._adaptive_catchup = adaptive_catchup
        self._effective_scroll_speed = scroll_speed

        self.setWindowFlag(Qt.FramelessWindowHint, True)
        self.setWindowFlag(Qt.Tool, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.set_always_on_top(always_on_top)

        self._scroll_timer = QTimer(self)
        self._scroll_timer.setInterval(FRAME_MS)
        self._scroll_timer.timeout.connect(self._advance_scroll)

        self._apply_geometry()
        self._sync_scroll_timer()

    # ── colours ──────────────────────────────────────────────────────────
    @property
    def _colors(self) -> dict[str, str]:
        return palette(self._theme_mode)

    def _translation_qcolor(self) -> QColor:
        return QColor(self._translation_color or self._colors["text"])

    def _source_qcolor(self) -> QColor:
        return QColor(self._source_color or self._colors["muted"])

    def _transparent_static_active(self) -> bool:
        """Transparent backdrop is a static-mode option only."""
        return self._mode == SUBTITLE_MODE_STATIC and self._transparent_static

    def _backdrop(self) -> QColor:
        """Window backdrop, drawn behind everything else."""
        if self._transparent_static_active():
            # Fully transparent: contrast comes from per-line cards instead, so
            # the text stays readable over arbitrary video.
            return QColor(0, 0, 0, 0)
        base = QColor(self._colors["app_bg"])
        base.setAlpha(190)
        return base

    # ── geometry ─────────────────────────────────────────────────────────
    def _screen(self):
        screens = QGuiApplication.screens()
        if not screens:
            return None
        idx = max(0, min(self._monitor_index, len(screens) - 1))
        return screens[idx]

    def _apply_geometry(self) -> None:
        """Occupy the bottom ``height_percent`` of the chosen screen.

        Qt reports each screen's geometry in logical units already, so there is
        no DPI arithmetic here — that is what gui/scaling.py does by hand.
        """
        screen = self._screen()
        if screen is None:
            return
        g = screen.geometry()
        h = max(1, int(g.height() * self._height_percent / 100))
        self.setGeometry(QRect(g.x(), g.y() + g.height() - h, g.width(), h))

    # ── layout helpers ───────────────────────────────────────────────────
    def _content_width(self) -> int:
        return max(1, int(self.width() * (1 - 2 * SIDE_MARGIN_RATIO)))

    # ``font_size_base`` and ``source_font_size_base`` are DIVISORS, not pixel
    # sizes: the rendered size is the window width divided by the base, so text
    # keeps its proportion on any monitor. Smaller base => larger text.
    def _translation_px(self) -> int:
        if not self.width():
            return 24
        return max(12, min(120, int(self.width() / self._font_size_base)))

    def _source_px(self) -> int:
        if not self.width():
            return 17
        return max(12, min(120, int(self.width() / self._source_font_size_base)))

    def _measure(self, text: str, font: QFont) -> int:
        """Wrapped pixel height of ``text`` at ``font`` within the content width.

        Qt measures after shaping and bidi, so this is correct for Arabic
        without the caller knowing anything about the script.
        """
        fm = QFontMetrics(font)
        rect = fm.boundingRect(
            QRect(0, 0, self._content_width(), 10_000),
            int(Qt.TextWordWrap | Qt.AlignHCenter),
            text,
        )
        return rect.height()

    def _block_fonts(self, block: Block) -> tuple[QFont, QFont | None]:
        trans = subtitle_font(self._translation_px())
        src = None
        if self._bilingual and block.source:
            src = source_font(self._source_px(), block.source)
        return trans, src

    def _measure_block(self, block: Block) -> int:
        trans_font, src_font = self._block_fonts(block)
        h = self._measure(block.translation, trans_font)
        if src_font is not None and block.source:
            h += self._measure(block.source, src_font) + PAIR_GAP
        return h

    def _draw_card(self, p: QPainter, text: str, font: QFont, rect: QRect) -> None:
        """Rounded backing card behind one line, sized to the text it holds.

        Only used when the backdrop is transparent: without it the subtitle
        would have to compete with whatever video is underneath.
        """
        fm = QFontMetrics(font)
        tw = min(fm.horizontalAdvance(text), rect.width())
        pad_x, pad_y = 20, 8
        cw = min(rect.width(), tw + pad_x * 2)
        cx = rect.x() + (rect.width() - cw) // 2
        path = QPainterPath()
        path.addRoundedRect(cx, rect.y() - pad_y, cw, rect.height() + pad_y * 2, 14, 14)
        p.fillPath(path, QColor(0, 0, 0, 150))

    def _draw_block(self, p: QPainter, block: Block, x: int, y: int) -> int:
        """Draw ``block`` with its top edge at ``y``; return the height used."""
        trans_font, src_font = self._block_fonts(block)
        w = self._content_width()
        cards = self._transparent_static_active()
        used = 0
        if src_font is not None and block.source:
            sh = self._measure(block.source, src_font)
            rect = QRect(x, y, w, sh)
            if cards:
                self._draw_card(p, block.source, src_font, rect)
            p.setFont(src_font)
            p.setPen(self._source_qcolor())
            p.drawText(
                rect, int(Qt.TextWordWrap | Qt.AlignHCenter | Qt.AlignTop), block.source
            )
            used += sh + PAIR_GAP
        th = self._measure(block.translation, trans_font)
        rect = QRect(x, y + used, w, th)
        if cards:
            self._draw_card(p, block.translation, trans_font, rect)
        p.setFont(trans_font)
        p.setPen(self._translation_qcolor())
        p.drawText(
            rect,
            int(Qt.TextWordWrap | Qt.AlignHCenter | Qt.AlignTop),
            block.translation,
        )
        return used + th

    # ── painting ─────────────────────────────────────────────────────────
    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        p.fillRect(self.rect(), self._backdrop())

        if self._announcement:
            self._paint_announcement(p)
        elif self._mode == SUBTITLE_MODE_REALTIME:
            self._paint_realtime(p)
        elif self._mode == SUBTITLE_MODE_CONTINUOUS:
            self._paint_continuous(p)
        else:
            self._paint_static(p)

        # Pills paint last so they always sit above subtitle text. In the Tk
        # version z-order followed canvas item creation order, so every new
        # item had to be followed by an explicit _raise_footer() call.
        self._paint_pills(p)

    def _paint_realtime(self, p: QPainter) -> None:
        """Top-down feed: settled blocks stack from the top, live line below."""
        x = int(self.width() * SIDE_MARGIN_RATIO)
        y = int(self.height() * 0.06 - self._scroll_offset)
        for block in self._blocks:
            h = self._draw_block(p, block, x, y)
            y += h + REALTIME_BLOCK_SPACING
        if self._live_text:
            self._draw_live_line(p, x, y)

    def _continuous_positions(self) -> list[tuple[Block, int, int]]:
        """(block, height, y) for continuous mode, in feed order.

        Each block enters from just below the visible area and rises as the
        scroll offset grows, so text waiting behind the viewport is genuinely
        queued rather than re-anchored to the bottom every frame. That queue is
        what ``get_subtitle_backlog_count`` measures and adaptive catch-up drains.
        """
        out: list[tuple[Block, int, int]] = []
        y = self._content_height() - int(self._scroll_offset)
        for block in self._blocks:
            h = self._measure_block(block)
            out.append((block, h, y))
            y += h + REALTIME_BLOCK_SPACING
        return out

    def _paint_continuous(self, p: QPainter) -> None:
        """Steady upward scroll; new text enters from the bottom edge."""
        x = int(self.width() * SIDE_MARGIN_RATIO)
        positions = self._continuous_positions()
        limit = self._content_height()

        evicted = 0
        for block, h, y in positions:
            if y + h < 0:  # scrolled fully past the top edge
                evicted += 1
                continue
            if y > limit:  # still queued below the viewport
                break
            self._draw_block(p, block, x, y)

        if evicted:
            # Drop off-screen blocks and shorten the offset by exactly the
            # extent removed, so the survivors do not jump.
            drop = sum(h + REALTIME_BLOCK_SPACING for _, h, _ in positions[:evicted])
            del self._blocks[:evicted]
            self._scroll_offset -= drop

    def _paint_static(self, p: QPainter) -> None:
        """Only the newest block, vertically centred."""
        if not self._blocks:
            return
        block = self._blocks[-1]
        x = int(self.width() * SIDE_MARGIN_RATIO)
        h = self._measure_block(block)
        self._draw_block(p, block, x, max(0, (self._content_height() - h) // 2))

    def _draw_live_line(self, p: QPainter, x: int, y: int) -> None:
        """In-progress transcript: muted while speaking, primary once settled."""
        text = self._live_text or ""
        font = source_font(self._font_size_base, text)
        fm = QFontMetrics(font)
        # Show only the newest row: a long interim that wrapped would otherwise
        # shove the settled history up by several rows at once.
        max_h = fm.lineSpacing() * REALTIME_LIVE_MAX_ROWS
        p.setFont(font)
        p.setPen(self._translation_qcolor() if self._live_settled else self._source_qcolor())
        p.drawText(
            QRect(x, y, self._content_width(), max_h),
            int(Qt.AlignHCenter | Qt.AlignTop),
            fm.elidedText(text, Qt.ElideLeft, self._content_width()),
        )

    def _pill_font(self) -> QFont:
        return subtitle_font(max(14, self._translation_px() // 3), bold=False)

    def _pill_height(self) -> int:
        return QFontMetrics(self._pill_font()).height() + 16

    def _footer_text(self) -> str:
        from gui.subtitle_window import DEFAULT_FOOTER, FOOTER_TRANSLATIONS

        return FOOTER_TRANSLATIONS.get(self._target_language, DEFAULT_FOOTER)

    def _stopped_text(self) -> str:
        from utils.user_messages import get_user_message

        # Reads the target language from settings itself, so it re-localises
        # whenever the pill is repainted.
        return get_user_message("app_stopped")

    def reserved_bottom(self) -> int:
        """Height at the bottom the pills occupy — subtitles must stay above it.

        Computed before any content is laid out, so a block can never be drawn
        underneath the disclaimer.
        """
        r = 0
        if self._show_footer:
            r += self._pill_height() + FOOTER_MARGIN
        if self._stopped_hint:
            r += self._pill_height() + PILL_GAP
        return r

    def _content_height(self) -> int:
        return max(1, self.height() - self.reserved_bottom())

    def _pill(self, p: QPainter, text: str, bottom: int, fill: QColor, fg: QColor) -> int:
        """Draw a centred rounded pill with its bottom edge at ``bottom``."""
        font = self._pill_font()
        fm = QFontMetrics(font)
        w = fm.horizontalAdvance(text) + 36
        h = self._pill_height()
        x = (self.width() - w) // 2
        y = bottom - h
        path = QPainterPath()
        path.addRoundedRect(x, y, w, h, h / 2, h / 2)
        p.fillPath(path, fill)
        p.setFont(font)
        p.setPen(fg)
        p.drawText(QRect(x, y, w, h), int(Qt.AlignCenter), text)
        return y

    def _paint_pills(self, p: QPainter) -> None:
        """Footer last-but-one, stopped hint stacked directly above it."""
        bottom = self.height() - FOOTER_MARGIN
        if self._show_footer:
            bottom = self._pill(
                p,
                self._footer_text(),
                bottom,
                QColor(self._colors["warning"]),
                QColor("#111827"),
            ) - PILL_GAP
        if self._stopped_hint:
            c = self._colors
            self._pill(p, self._stopped_text(), bottom, QColor(c["card"]), QColor(c["muted"]))

    def _paint_announcement(self, p: QPainter) -> None:
        block = Block(self._announcement or "")
        x = int(self.width() * SIDE_MARGIN_RATIO)
        h = self._measure_block(block)
        self._draw_block(p, block, x, max(0, (self._content_height() - h) // 2))

    # ── scrolling ────────────────────────────────────────────────────────
    def _sync_scroll_timer(self) -> None:
        if self._mode == SUBTITLE_MODE_CONTINUOUS:
            self._scroll_timer.start()
        else:
            self._scroll_timer.stop()

    def get_subtitle_backlog_count(self) -> int:
        """How many blocks are queued below the visible anchor line.

        Continuous mode only — the other modes do not queue.
        """
        if self._mode != SUBTITLE_MODE_CONTINUOUS:
            return 0
        limit = self._content_height()
        return sum(1 for _, _, y in self._continuous_positions() if y > limit)

    def _current_scroll_speed(self) -> float:
        """Smoothed scroll speed, with optional readability-first catch-up.

        Accelerates gently and caps at 2x so a backlog is worked off without
        the text becoming unreadable, then EMA-smoothed so the speed change
        itself is not visible as a jolt.
        """
        target = self._scroll_speed
        if self._adaptive_catchup and self._mode == SUBTITLE_MODE_CONTINUOUS:
            target *= min(2.0, 1.0 + 0.25 * self.get_subtitle_backlog_count())
        self._effective_scroll_speed = (
            0.85 * self._effective_scroll_speed + 0.15 * target
        )
        return self._effective_scroll_speed

    def _advance_scroll(self) -> None:
        self._scroll_offset += SCROLL_PIXELS_PER_FRAME * self._current_scroll_speed()
        self.update()

    # ── public API ───────────────────────────────────────────────────────
    def add_subtitle(self, text: str, source_text: str | None = None) -> None:
        """Append a settled utterance.

        Long realtime blocks are split at sentence boundaries so one 12 s
        utterance does not land as a wall of text. Display-only: no extra API
        calls, and a bilingual pair is never split (the halves have no
        per-sentence alignment).
        """
        if not text:
            return
        from gui.subtitle_window import split_display_chunks

        if self._mode == SUBTITLE_MODE_REALTIME and not source_text:
            chunks = split_display_chunks(text, REALTIME_MAX_BLOCK_CHARS)
        else:
            chunks = [text]
        for i, chunk in enumerate(chunks):
            self._blocks.append(Block(chunk, source_text if i == 0 else None))
        self.update()

    def set_live_text(self, text: str | None, settled: bool = False) -> None:
        self._live_text = text or None
        self._live_settled = settled
        self.update()

    def set_subtitle_mode(self, mode: str) -> None:
        self._mode = mode
        self._scroll_offset = 0.0
        self._sync_scroll_timer()
        self.update()

    def get_subtitle_mode(self) -> str:
        return self._mode

    def set_bilingual_mode(self, enabled: bool) -> None:
        self._bilingual = enabled
        self.update()

    def set_theme(self, theme_mode: str) -> None:
        self._theme_mode = theme_mode
        self.update()

    def set_language(self, language: str) -> None:
        self._target_language = language
        self.update()

    def set_show_footer(self, enabled: bool) -> None:
        self._show_footer = enabled
        self.update()

    def set_stopped_hint(self, visible: bool) -> None:
        self._stopped_hint = visible
        self.update()

    def set_announcement(self, text: str) -> None:
        self._announcement = text
        self.update()

    def clear_announcement(self) -> None:
        self._announcement = None
        self.update()

    def set_monitor(self, monitor_index: int) -> None:
        self._monitor_index = monitor_index
        self._apply_geometry()
        self.update()

    def set_window_height_percent(self, percent: int) -> None:
        self._height_percent = percent
        self._apply_geometry()
        self.update()

    def set_always_on_top(self, enabled: bool) -> None:
        self.setWindowFlag(Qt.WindowStaysOnTopHint, enabled)
        if self.isVisible():
            self.show()  # re-apply flags without losing visibility

    def set_transparent_static(self, enabled: bool) -> None:
        self._transparent_static = enabled
        self.update()

    def set_translation_text_color(self, color: str | None) -> None:
        self._translation_color = color or ""
        self.update()

    def set_source_text_color(self, color: str | None) -> None:
        self._source_color = color or ""
        self.update()

    def get_translation_text_color(self) -> str:
        return self._translation_color or self._colors["text"]

    def get_source_text_color(self) -> str:
        return self._source_color or self._colors["muted"]

    # The base is a divisor, so "increase font" LOWERS it. Bounds match the Tk
    # version (20-80 for the translation, 20-120 for the original text).
    def increase_font(self) -> None:
        self._font_size_base = max(20, self._font_size_base - 5)
        self.update()

    def decrease_font(self) -> None:
        self._font_size_base = min(80, self._font_size_base + 5)
        self.update()

    def get_font_size_base(self) -> int:
        return self._font_size_base

    def get_current_font_size(self) -> int:
        return self._translation_px()

    def set_source_font_size_base(self, value: float) -> None:
        try:
            self._source_font_size_base = max(20.0, min(120.0, float(value)))
        except (TypeError, ValueError):
            return
        self.update()

    def increase_source_font(self) -> None:
        self.set_source_font_size_base(self._source_font_size_base - 5.0)

    def decrease_source_font(self) -> None:
        self.set_source_font_size_base(self._source_font_size_base + 5.0)

    def get_source_font_size_base(self) -> float:
        return self._source_font_size_base

    def get_current_source_font_size(self) -> int:
        return self._source_px()

    def increase_scroll_speed(self) -> float:
        self._scroll_speed = min(5.0, self._scroll_speed + 0.25)
        return self._scroll_speed

    def decrease_scroll_speed(self) -> float:
        self._scroll_speed = max(0.25, self._scroll_speed - 0.25)
        return self._scroll_speed

    def set_adaptive_catchup(self, enabled: bool) -> None:
        self._adaptive_catchup = enabled

    def clear(self) -> None:
        self._blocks.clear()
        self._live_text = None
        self._scroll_offset = 0.0
        self._effective_scroll_speed = self._scroll_speed
        self.update()

    def hide(self) -> None:
        """Hide the overlay and stop animating.

        The live line is dropped: no further interims will arrive to correct
        it, so leaving it on screen would strand a half-finished sentence.
        """
        self._scroll_timer.stop()
        self._live_text = None
        super().hide()

    def show(self) -> None:
        super().show()
        self._apply_geometry()
        self._sync_scroll_timer()

    def destroy(self) -> None:
        """Tear the overlay down. Named to match the Tk window's API."""
        self._scroll_timer.stop()
        self.close()
        self.deleteLater()

    def closeEvent(self, event) -> None:
        # Closing the overlay stops the session; it never quits the app. The Tk
        # version wired this to full shutdown, so an Alt+F4 on the OBS-visible
        # overlay took the whole app down (fixed in PR #24).
        if self._on_stop:
            self._on_stop()
        if self._on_close:
            self._on_close()
        super().closeEvent(event)
