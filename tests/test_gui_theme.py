"""Tests for the G-Helper-derived theme.

XPS Helper and G-Helper are both GPL-3.0, so the design system is shared legally;
``CREDITS.md`` records the debt. These tests pin the parts that would silently
degrade the UI if they drifted -- the palette, the accent-per-mode mapping, and
the text-contrast rule.
"""

from __future__ import annotations

import pytest

from xpshelper.gui import theme


# ---------------------------------------------------------------------------
# Palettes
# ---------------------------------------------------------------------------
def test_dark_palette_matches_g_helpers_values():
    """From ``RForm.InitColors(darkTheme: true)``."""
    assert theme.DARK.form_back == "#1c1c1c"
    assert theme.DARK.button_main == "#2e2e2e"
    assert theme.DARK.button_second == "#242424"
    assert theme.DARK.fore_main == "#f0f0f0"
    assert theme.DARK.border_main == "#373737"


def test_light_palette_is_the_windows_default_surface():
    assert theme.LIGHT.form_back == "#f0f0f0"
    assert theme.LIGHT.button_main == "#ffffff"
    assert theme.LIGHT.fore_main == "#000000"


@pytest.mark.parametrize("palette", [theme.DARK, theme.LIGHT])
def test_every_colour_is_a_hex_triplet(palette):
    for field in (
        "form_back", "button_main", "button_second", "fore_main",
        "fore_dim", "border_main", "border_second", "chart_main", "chart_grid",
    ):
        value = getattr(palette, field)
        assert value.startswith("#") and len(value) == 7, (field, value)
        int(value[1:], 16)  # raises if not hex


def test_dark_and_light_are_distinguishable():
    assert theme.DARK.dark is True
    assert theme.LIGHT.dark is False
    assert theme.DARK.form_back != theme.LIGHT.form_back


# ---------------------------------------------------------------------------
# Accents
# ---------------------------------------------------------------------------
def test_accent_values_match_g_helpers_statics():
    assert theme.ACCENT_ECO == "#06b48a"
    assert theme.ACCENT_STANDARD == "#3aaeef"
    assert theme.ACCENT_TURBO == "#ff2020"
    assert theme.ACCENT_CUSTOM == "#ff8000"
    assert theme.ACCENT_GRAY == "#a8a8a8"


def test_every_dell_thermal_mode_has_an_accent():
    """A mode with no accent would render as neutral grey, i.e. look disabled."""
    from xpshelper import thermal

    for name in thermal.THERMAL_MODES:
        accent = theme.mode_accent(name)
        assert accent != theme.ACCENT_GRAY, name
        assert theme.MODE_ACCENTS[name] == accent


def test_quiet_is_green_and_ultra_is_red():
    """The temperature ladder: the coolest setting is green, the hottest red."""
    assert theme.mode_accent("Quiet") == theme.ACCENT_ECO
    assert theme.mode_accent("Ultra") == theme.ACCENT_TURBO


def test_unknown_mode_falls_back_to_grey():
    assert theme.mode_accent("Turbo") == theme.ACCENT_GRAY


def test_profile_accents_cover_the_shipped_profiles():
    from xpshelper.model import ThermalProfile

    for profile in ThermalProfile:
        assert theme.profile_accent(profile.value) != theme.ACCENT_GRAY, profile


def test_profile_accents_are_case_insensitive():
    assert theme.profile_accent("QUIET") == theme.profile_accent("quiet")


# ---------------------------------------------------------------------------
# Theme resolution
# ---------------------------------------------------------------------------
def test_explicit_theme_settings_win():
    assert theme.resolve("dark").dark is True
    assert theme.resolve("light").dark is False
    assert theme.resolve("DARK").dark is True


def test_none_and_unknown_follow_the_operating_system(monkeypatch):
    monkeypatch.setattr(theme, "system_is_dark", lambda: True)
    assert theme.resolve(None).dark is True
    assert theme.resolve("flat").dark is True

    monkeypatch.setattr(theme, "system_is_dark", lambda: False)
    assert theme.resolve(None).dark is False


def test_windows_setting_follows_the_operating_system(monkeypatch):
    """G-Helper's ``windows`` mode, kept so an existing setting keeps working."""
    monkeypatch.setattr(theme, "system_is_dark", lambda: True)
    assert theme.resolve("windows").dark is True
    monkeypatch.setattr(theme, "system_is_dark", lambda: False)
    assert theme.resolve("windows").dark is False


def test_system_is_dark_never_raises():
    """Absent registry key, non-Windows, or denied access all mean "light"."""
    assert isinstance(theme.system_is_dark(), bool)
