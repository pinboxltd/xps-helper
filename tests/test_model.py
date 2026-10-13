"""Model tests: firmware tokens, aliases, and Dell's threshold rules."""

from __future__ import annotations

import pytest

from xpshelper.errors import ValidationError
from xpshelper.model import (
    ChargeConfig,
    ChargeMode,
    ThermalProfile,
    parse_charge_mode_list,
    parse_profile_list,
)


# ---------------------------------------------------------------------------
# Firmware tokens must match Dell exactly.
# ---------------------------------------------------------------------------
def test_thermal_tokens_match_dell():
    assert [p.value for p in ThermalProfile] == [
        "Optimized",
        "Quiet",
        "Cool",
        "UltraPerformance",
    ]


def test_charge_tokens_match_dell():
    assert [m.value for m in ChargeMode] == [
        "Standard",
        "Express",
        "PrimAcUse",
        "Adaptive",
        "Custom",
    ]


def test_no_enum_member_looks_like_a_private_attribute():
    """Regression guard for a real CPython trap.

    A ``_ALIASES = {...}`` class attribute inside an Enum body is silently
    turned into an enum *member*, which would add a bogus entry to
    ``tuple(ThermalProfile)`` and to every capability list built from it.
    Alias data therefore lives in module-level dicts looked up via a classmethod.
    """
    for enum_cls in (ThermalProfile, ChargeMode):
        for member in enum_cls:
            assert not member.name.startswith("_"), (
                f"{enum_cls.__name__}.{member.name} looks like a class attribute "
                "that got turned into a member"
            )


# ---------------------------------------------------------------------------
# Lenient input parsing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,expected",
    [
        ("Optimized", ThermalProfile.OPTIMIZED),
        ("optimized", ThermalProfile.OPTIMIZED),
        (" balanced ", ThermalProfile.OPTIMIZED),
        ("Quiet", ThermalProfile.QUIET),
        ("silent", ThermalProfile.QUIET),
        ("UltraPerformance", ThermalProfile.ULTRA_PERFORMANCE),
        ("ultra performance", ThermalProfile.ULTRA_PERFORMANCE),
        ("ULTRA-PERFORMANCE", ThermalProfile.ULTRA_PERFORMANCE),
        ("performance", ThermalProfile.ULTRA_PERFORMANCE),
        ("turbo", ThermalProfile.ULTRA_PERFORMANCE),
        ("cool", ThermalProfile.COOL),
    ],
)
def test_profile_aliases(text, expected):
    assert ThermalProfile.from_token(text) is expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("PrimAcUse", ChargeMode.PRIMARILY_AC),
        ("primarily-ac", ChargeMode.PRIMARILY_AC),
        ("Primarily AC Use", ChargeMode.PRIMARILY_AC),
        ("primarily_ac", ChargeMode.PRIMARILY_AC),
        ("Express", ChargeMode.EXPRESS),
        ("express charge", ChargeMode.EXPRESS),
        ("Standard", ChargeMode.STANDARD),
        ("adaptive", ChargeMode.ADAPTIVE),
        ("custom", ChargeMode.CUSTOM),
        # Dell reports values with a parameter suffix in capability lists.
        ("Custom:50-95", ChargeMode.CUSTOM),
    ],
)
def test_charge_aliases(text, expected):
    assert ChargeMode.from_token(text) is expected


@pytest.mark.parametrize("text", ["", "   ", "nonsense", "quietish", None, "OptimizedX"])
def test_unknown_tokens_return_none(text):
    assert ThermalProfile.from_token(text) is None


def test_parse_profile_list_drops_unknown_and_dedupes():
    parsed = parse_profile_list(["Quiet", "quiet", "Banana", "Cool"])
    assert parsed == (ThermalProfile.QUIET, ThermalProfile.COOL)


def test_parse_charge_mode_list_handles_suffixed_custom():
    parsed = parse_charge_mode_list(["Standard", "Custom:50-95"])
    assert parsed == (ChargeMode.STANDARD, ChargeMode.CUSTOM)


def test_power_capping_profiles():
    assert ThermalProfile.QUIET.caps_cpu_power
    assert ThermalProfile.COOL.caps_cpu_power
    assert not ThermalProfile.OPTIMIZED.caps_cpu_power
    assert not ThermalProfile.ULTRA_PERFORMANCE.caps_cpu_power


# ---------------------------------------------------------------------------
# Custom threshold validation -- Dell's own documented rules.
# ---------------------------------------------------------------------------
def test_custom_charge_config_token():
    config = ChargeConfig(mode=ChargeMode.CUSTOM, start=60, stop=80)
    assert config.token == "Custom:60-80"
    assert "60%" in config.describe and "80%" in config.describe


def test_simple_charge_config_token():
    assert ChargeConfig(mode=ChargeMode.PRIMARILY_AC).token == "PrimAcUse"


@pytest.mark.parametrize(
    "start,stop",
    [
        (49, 80),   # start below the documented minimum
        (96, 100),  # start above the documented maximum
        (50, 54),   # stop below the documented minimum
        (50, 101),  # stop above the documented maximum
        (80, 84),   # gap smaller than 5
    ],
)
def test_custom_thresholds_outside_dell_rules_rejected(start, stop):
    with pytest.raises(ValidationError):
        ChargeConfig(mode=ChargeMode.CUSTOM, start=start, stop=stop)


@pytest.mark.parametrize("start,stop", [(50, 55), (95, 100), (60, 80)])
def test_custom_thresholds_at_boundaries_accepted(start, stop):
    config = ChargeConfig(mode=ChargeMode.CUSTOM, start=start, stop=stop)
    assert config.start == start and config.stop == stop


def test_custom_requires_both_thresholds():
    with pytest.raises(ValidationError, match="requires both"):
        ChargeConfig(mode=ChargeMode.CUSTOM, start=60)
    with pytest.raises(ValidationError, match="requires both"):
        ChargeConfig(mode=ChargeMode.CUSTOM)


def test_non_custom_rejects_thresholds():
    with pytest.raises(ValidationError, match="only apply to Custom"):
        ChargeConfig(mode=ChargeMode.STANDARD, start=60, stop=80)


def test_charge_config_from_token_round_trip():
    parsed = ChargeConfig.from_token("Custom:75-80")
    assert parsed == ChargeConfig(mode=ChargeMode.CUSTOM, start=75, stop=80)
    assert ChargeConfig.from_token("Standard") == ChargeConfig(mode=ChargeMode.STANDARD)
    assert ChargeConfig.from_token("PrimAcUse") == ChargeConfig(mode=ChargeMode.PRIMARILY_AC)


@pytest.mark.parametrize("text", ["", "Custom:abc", "Custom:60", "Custom", "nonsense", "Custom:80-60"])
def test_charge_config_from_token_rejects_bad_input(text):
    assert ChargeConfig.from_token(text) is None
