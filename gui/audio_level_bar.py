"""Compact segmented audio-level bar for the control panel."""

from __future__ import annotations

import customtkinter as ctk


class AudioLevelBar(ctk.CTkFrame):
    """Render one normalized level across green, amber, and red zones.

    A normal progress bar can only use one colour at a time.  Audio meters are
    easier to read when their fixed zones stay meaningful while the fill
    moves through them, so this widget composes three adjacent progress bars
    behind the same small ``set``/``get`` interface.
    """

    GREEN_END = 0.70  # -18 dBFS on the GUI's -60..0 dB scale
    RED_START = 5.0 / 6.0  # -10 dBFS: the GUI's "high" threshold

    def __init__(
        self,
        master,
        *,
        track_color: str,
        border_color: str,
        green_color: str,
        warning_color: str,
        danger_color: str,
        height: int = 11,
    ) -> None:
        super().__init__(
            master,
            width=1,
            height=height,
            corner_radius=max(1, height // 2),
            border_width=1,
            border_color=border_color,
            fg_color=track_color,
        )
        self._value = 0.0
        self._ranges = (
            (0.0, self.GREEN_END),
            (self.GREEN_END, self.RED_START),
            (self.RED_START, 1.0),
        )
        weights = tuple(
            max(1, round((end - start) * 100)) for start, end in self._ranges
        )
        for column, weight in enumerate(weights):
            self.grid_columnconfigure(column, weight=weight, uniform="level-zone")
        self.grid_rowconfigure(0, weight=1)

        self._segments: list[ctk.CTkProgressBar] = []
        for column, color in enumerate(
            (green_color, warning_color, danger_color)
        ):
            segment = ctk.CTkProgressBar(
                self,
                width=1,
                height=max(3, height - 4),
                corner_radius=max(1, (height - 4) // 2),
                border_width=0,
                fg_color=track_color,
                progress_color=color,
            )
            segment.grid(
                row=0,
                column=column,
                sticky="ew",
                padx=(2 if column == 0 else 0, 2 if column == 2 else 1),
                pady=2,
            )
            segment.set(0.0)
            self._segments.append(segment)

    @staticmethod
    def _clamp(value: float) -> float:
        return max(0.0, min(1.0, float(value)))

    def set(self, value: float) -> None:
        """Set the total normalized fill across all three zones."""

        self._value = self._clamp(value)
        for segment, (start, end) in zip(
            self._segments, self._ranges, strict=True
        ):
            segment.set(self._clamp((self._value - start) / (end - start)))

    def get(self) -> float:
        """Return the total normalized fill (matching ``CTkProgressBar``)."""

        return self._value

    @property
    def segment_values(self) -> tuple[float, float, float]:
        """Expose zone fills for focused GUI regression tests."""

        return tuple(segment.get() for segment in self._segments)  # type: ignore[return-value]

    def set_palette(
        self,
        *,
        track_color: str,
        border_color: str,
        green_color: str,
        warning_color: str,
        danger_color: str,
    ) -> None:
        """Apply a light/dark palette without changing the displayed level."""

        self.configure(fg_color=track_color, border_color=border_color)
        for segment, color in zip(
            self._segments,
            (green_color, warning_color, danger_color),
            strict=True,
        ):
            segment.configure(fg_color=track_color, progress_color=color)
