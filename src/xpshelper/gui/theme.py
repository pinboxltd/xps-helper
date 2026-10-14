"""Visual design, ported from G-Helper's theme tokens.

XPS Helper and [G-Helper](https://github.com/seerge/g-helper) are both GPL-3.0, so
borrowing the design system is fine; the debt is recorded in ``CREDITS.md``.

What was taken is the *token set and control behaviour*, not code: the colour
table, the accent-per-mode convention, and the way controls restyle themselves on
a theme switch. G-Helper's originals are C#/WinForms (``RForm``,
``ControlHelper``, ``RButton``); this is a re-implementation against tkinter, so
nothing transfers literally anyway.

The design
----------
- A single dark or light **surface** behind everything.
- **Group headers** sit on a slightly darker strip than the surface.
- **Buttons** are flat with an optional 1px border, never beveled.
- Each performance mode owns an **accent colour**, and the active mode is the only
  thing rendered in it. That is what makes the window readable at a glance: the
  colour *is* the state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# The colour table
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Palette:
    """One theme's colours, named after G-Helper's ``RForm`` fields."""

    #: The window surface.
    form_back: str
    #: Primary button fill.
    button_main: str
    #: Secondary fill, used for group headers and inset rows.
    button_second: str
    #: Body text.
    fore_main: str
    #: Subdued text for hints.
    fore_dim: str
    #: Border around a primary control.
    border_main: str
    #: Border around an inset control.
    border_second: str
    #: Chart/plot background.
    chart_main: str
    #: Chart gridlines.
    chart_grid: str

    @property
    def dark(self) -> bool:
        return self is DARK


#: From ``RForm.InitColors(darkTheme: true)``.
DARK = Palette(
    form_back="#1c1c1c",
    button_main="#2e2e2e",
    button_second="#242424",
    fore_main="#f0f0f0",
    fore_dim="#a8a8a8",
    border_main="#373737",
    border_second="#2a2a2a",
    chart_main="#232323",
    chart_grid="#464646",
)

#: The light half of ``RForm.InitColors``. G-Helper uses the Windows system
#: colours here; these are their standard values so the theme is reproducible
#: without querying the OS.
LIGHT = Palette(
    form_back="#f0f0f0",
    button_main="#ffffff",
    button_second="#e5e5e5",
    fore_main="#000000",
    fore_dim="#606060",
    border_main="#dcdcdc",
    border_second="#d7d7d7",
    chart_main="#ffffff",
    chart_grid="#d3d3d3",
)


# ---------------------------------------------------------------------------
# Accent colours
# ---------------------------------------------------------------------------

#: From ``RForm``'s static accent fields.
ACCENT_ECO = "#06b48a"       # colorEco   -- G-Helper's power-saving green
ACCENT_STANDARD = "#3aaeef"  # colorStandard -- its balanced blue
ACCENT_TURBO = "#ff2020"     # colorTurbo  -- its performance red
ACCENT_CUSTOM = "#ff8000"    # colorCustom -- its custom orange
ACCENT_GRAY = "#a8a8a8"      # colorGray   -- neutral / unavailable

#: Accent per Dell thermal mode, using G-Helper's convention where the coolest and
#: quietest settings are green and the hottest is red. Reading the row left to
#: right goes green -> blue -> cyan -> red, which matches the temperature ladder.
MODE_ACCENTS: dict[str, str] = {
    "Balanced": ACCENT_STANDARD,
    "Cool": "#22c1c3",
    "Quiet": ACCENT_ECO,
    "Ultra": ACCENT_TURBO,
}

#: Accent per BIOS thermal profile, keyed by the normalised token.
#:
#: ``ThermalProfile.ULTRA_PERFORMANCE`` has the value ``"UltraPerformance"``, with
#: no separator, so the lookup normalises rather than relying on punctuation
#: matching: lower-case and drop everything that is not alphanumeric.
PROFILE_ACCENTS: dict[str, str] = {
    "quiet": ACCENT_ECO,
    "silent": ACCENT_ECO,
    "cool": "#22c1c3",
    "optimized": ACCENT_STANDARD,
    "balanced": ACCENT_STANDARD,
    "ultraperformance": ACCENT_TURBO,
    "performance": ACCENT_TURBO,
    "turbo": ACCENT_TURBO,
}


def _key(value) -> str:
    """Normalise a token for lookup: lower-case, alphanumerics only."""
    inner = getattr(value, "value", value)
    return "".join(ch for ch in str(inner).lower() if ch.isalnum())


def mode_accent(name: str) -> str:
    """Accent for a thermal-mode name, falling back to neutral grey."""
    return MODE_ACCENTS.get(str(name), ACCENT_GRAY)


def profile_accent(token) -> str:
    """Accent for a thermal-profile token or enum, falling back to grey.

    Accepts ``ThermalProfile`` members as well as their string values, so a
    caller cannot accidentally get grey by passing the enum instead of ``.value``.
    """
    return PROFILE_ACCENTS.get(_key(token), ACCENT_GRAY)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

#: Corner radius for rounded controls (``RComboBox.RoundedRect`` uses a similar
#: idea; tkinter has no native radius so widgets approximate it).
RADIUS = 6
#: Horizontal padding inside a button.
BUTTON_PADX = 14
#: Vertical padding inside a button.
BUTTON_PADY = 7
#: Padding around the window contents.
WINDOW_PAD = 14
#: Vertical gap between groups.
GROUP_GAP = 10


# ---------------------------------------------------------------------------
# Theme resolution
# ---------------------------------------------------------------------------

def system_is_dark() -> bool:
    """Whether Windows is currently in dark mode.

    Mirrors ``RForm.IsDarkTheme`` by reading the same registry value it does. Any
    failure means "light", matching its behaviour when the key is absent.
    """
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return int(value) <= 0
    except Exception:  # noqa: BLE001 - absent key, non-Windows, or denied
        return False


def resolve(theme: str | None) -> Palette:
    """Map a theme setting to a palette.

    Accepts G-Helper's setting names so an existing config carries over:
    ``dark``, ``light``, ``windows`` (follow the OS), or ``flat``/anything else for
    the default.
    """
    setting = (theme or "").strip().lower()
    if setting == "dark":
        return DARK
    if setting == "light":
        return LIGHT
    if setting == "windows":
        return DARK if system_is_dark() else LIGHT
    # Default to the OS, which is the least surprising for a first run.
    return DARK if system_is_dark() else LIGHT
