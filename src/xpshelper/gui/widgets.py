"""Flat, themed controls in the G-Helper style.

G-Helper's ``RButton`` is a flat button that takes its fill from the theme and
draws a 1px border, with a ``Secondary`` variant for inset rows and a
``Borderless`` variant for header/toolbar buttons. tkinter's ``ttk`` widgets
cannot do that: ``ttk`` styling is platform-theme bound, so a "flat dark button
with a coloured active state" is not expressible. These are plain ``tk`` widgets
on a ``Canvas``, which is the usual way to get that look.

The important behaviour, and the reason this is a widget rather than a styled
``ttk`` button: **the active option is the only one drawn in its accent colour.**
Everything else stays neutral, so the window reads at a glance.
"""

from __future__ import annotations

import logging
import tkinter as tk
import tkinter.font as tkfont
from typing import Callable

from . import theme

log = logging.getLogger(__name__)


class ModeButton(tk.Canvas):
    """A flat option button with an accent-coloured active state.

    Stands in for a radio button. A radio would need a separate indicator, and the
    whole point of this style is that the *fill* carries the state instead.
    """

    def __init__(
        self,
        master: tk.Misc,
        *,
        text: str,
        accent: str,
        palette: theme.Palette,
        command: Callable[[], None] | None = None,
        width: int | None = None,
        height: int = 30,
    ) -> None:
        self._text = text
        self._accent = accent
        self._palette = palette
        self._command = command
        self._selected = False
        self._enabled = True
        self._hover = False
        self._height = height

        font = ("Segoe UI", 9)
        # Measure the label so the button is exactly as wide as it needs to be,
        # which keeps a row of them evenly spaced instead of ragged.
        probe = tkfont.Font(font=font)
        text_width = probe.measure(text)
        self._width = width or (text_width + 2 * theme.BUTTON_PADX)

        super().__init__(
            master,
            width=self._width,
            height=height,
            highlightthickness=0,
            bd=0,
            bg=palette.form_back,
            cursor="hand2",
        )
        self._font = font

        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)
        self._redraw()

    # -- state ------------------------------------------------------------
    def set_selected(self, selected: bool) -> None:
        if self._selected != selected:
            self._selected = selected
            self._redraw()

    @property
    def selected(self) -> bool:
        return self._selected

    def set_enabled(self, enabled: bool) -> None:
        if self._enabled != enabled:
            self._enabled = enabled
            self.configure(cursor="hand2" if enabled else "arrow")
            self._redraw()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_palette(self, palette: theme.Palette) -> None:
        self._palette = palette
        self.configure(bg=palette.form_back)
        self._redraw()

    def set_text(self, text: str) -> None:
        self._text = text
        self._redraw()

    # -- events -----------------------------------------------------------
    def _on_enter(self, _event=None) -> None:
        if self._enabled:
            self._hover = True
            self._redraw()

    def _on_leave(self, _event=None) -> None:
        if self._hover:
            self._hover = False
            self._redraw()

    def _on_click(self, _event=None) -> None:
        if not self._enabled or self._command is None:
            return
        try:
            self._command()
        except Exception as exc:  # noqa: BLE001 - a UI callback must not kill Tk
            log.warning("mode button callback failed: %s", exc)

    # -- painting ---------------------------------------------------------
    def _colours(self) -> tuple[str, str, str]:
        """Return ``(fill, border, text)`` for the current state."""
        p = self._palette
        if not self._enabled:
            # Disabled reads as an unavailable mode, not as an error.
            return p.form_back, p.border_second, p.fore_dim
        if self._selected:
            return self._accent, self._accent, _contrast_on(self._accent)
        fill = p.button_second if self._hover else p.button_main
        return fill, p.border_main, p.fore_main

    def _redraw(self) -> None:
        self.delete("all")
        fill, border, fore = self._colours()
        w, h = self._width, self._height
        r = theme.RADIUS

        _rounded_rect(self, 1, 1, w - 1, h - 1, r, fill=fill, outline=border)
        self.create_text(
            w / 2, h / 2, text=self._text, fill=fore, font=self._font
        )


def _contrast_on(background: str) -> str:
    """Black or white text, whichever is legible on ``background``.

    G-Helper hardcodes white here; computing it means a light accent (custom
    orange) stays readable instead of turning into white-on-orange.
    """
    try:
        r, g, b = _hex_to_rgb(background)
    except ValueError:
        return "#ffffff"
    # Rec. 601 luma is good enough for a legibility call.
    luma = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return "#101010" if luma > 0.6 else "#ffffff"


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    v = value.lstrip("#")
    if len(v) != 6:
        raise ValueError(value)
    return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)


def _rounded_rect(
    canvas: tk.Canvas,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    r: int,
    *,
    fill: str,
    outline: str,
) -> None:
    """Draw a rounded rectangle as a smoothed polygon.

    tkinter has no arc-cornered rectangle primitive; a smoothed polygon with the
    corner points duplicated is the standard approximation.
    """
    r = max(0, min(r, (x2 - x1) // 2, (y2 - y1) // 2))
    if r == 0:
        canvas.create_rectangle(x1, y1, x2, y2, fill=fill, outline=outline)
        return
    points = [
        x1 + r, y1,
        x2 - r, y1,
        x2, y1,
        x2, y1 + r,
        x2, y2 - r,
        x2, y2,
        x2 - r, y2,
        x1 + r, y2,
        x1, y2,
        x1, y2 - r,
        x1, y1 + r,
        x1, y1,
    ]
    canvas.create_polygon(
        points, fill=fill, outline=outline, smooth=True, splinesteps=12, width=1
    )


class SectionHeader(tk.Frame):
    """A group heading on a contrasting strip, as G-Helper's header panels are."""

    def __init__(
        self,
        master: tk.Misc,
        *,
        text: str,
        palette: theme.Palette,
    ) -> None:
        super().__init__(master, bg=palette.button_second)
        self._palette = palette
        self._label = tk.Label(
            self,
            text=text,
            bg=palette.button_second,
            fg=palette.fore_main,
            font=("Segoe UI", 9, "bold"),
            anchor="w",
        )
        self._label.pack(fill="x", padx=theme.BUTTON_PADX, pady=4)

    def set_palette(self, palette: theme.Palette) -> None:
        self._palette = palette
        self.configure(bg=palette.button_second)
        self._label.configure(bg=palette.button_second, fg=palette.fore_main)


class Hint(tk.Label):
    """A subdued hint line."""

    def __init__(self, master: tk.Misc, *, palette: theme.Palette, text: str = "") -> None:
        super().__init__(
            master,
            text=text,
            bg=palette.form_back,
            fg=palette.fore_dim,
            font=("Segoe UI", 8),
            anchor="w",
            justify="left",
            wraplength=430,
        )

    def set_palette(self, palette: theme.Palette) -> None:
        self.configure(bg=palette.form_back, fg=palette.fore_dim)

    def set_text(self, text: str) -> None:
        self.configure(text=text)
