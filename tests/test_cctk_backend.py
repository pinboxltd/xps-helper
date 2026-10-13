"""cctk backend tests: output parsing, capability probing, exit-code mapping.

These run against canned Dell output via FakeRunner, so the logic that will face
real firmware is verified without the laptop.
"""

from __future__ import annotations

import sys

import pytest

from conftest import (
    CHARGE_HELP_FULL,
    THERMAL_HELP_FULL,
    THERMAL_HELP_PARTIAL,
    build_cctk,
)
from xpshelper import constants as C
from xpshelper.backends.cctk import (
    parse_attribute_value,
    parse_supported_arguments,
)
from xpshelper.errors import (
    AttributeNotSupported,
    BackendUnavailableError,
    BiosPasswordInvalid,
    BiosPasswordRequired,
    CctkError,
)
from xpshelper.model import ChargeConfig, ChargeMode, ThermalProfile


# ---------------------------------------------------------------------------
# Reading `cctk --Attribute`
# ---------------------------------------------------------------------------
def test_parse_attribute_value_simple():
    assert parse_attribute_value("ThermalManagement=Quiet", "ThermalManagement") == "Quiet"


def test_parse_attribute_value_is_case_insensitive_and_tolerates_noise():
    output = (
        "Dell Command | Configure\n"
        "\n"
        "thermalmanagement=UltraPerformance\n"
    )
    assert parse_attribute_value(output, "ThermalManagement") == "UltraPerformance"


def test_parse_attribute_value_ignores_unrelated_lines():
    output = "BiosVer=1.14.0\nThermalManagement=Cool\nSomethingElse=x"
    assert parse_attribute_value(output, "ThermalManagement") == "Cool"


def test_parse_attribute_value_missing_returns_none():
    assert parse_attribute_value("BiosVer=1.14.0", "ThermalManagement") is None
    assert parse_attribute_value("", "ThermalManagement") is None
    assert parse_attribute_value("ThermalManagement=", "ThermalManagement") is None


# ---------------------------------------------------------------------------
# Parsing `cctk -H --Attribute`
# ---------------------------------------------------------------------------
def test_parse_supported_arguments_pipe_format():
    values = parse_supported_arguments(THERMAL_HELP_FULL)
    assert values == {
        "optimized": True,
        "quiet": True,
        "cool": True,
        "ultraperformance": True,
    }


def test_parse_supported_arguments_marks_unsupported():
    values = parse_supported_arguments(THERMAL_HELP_PARTIAL)
    assert values["optimized"] is True
    assert values["cool"] is False


def test_parse_supported_arguments_comma_fallback():
    values = parse_supported_arguments("Arguments: optimized+, quiet+, cool")
    assert values == {"optimized": True, "quiet": True, "cool": False}


def test_parse_supported_arguments_whitespace_fallback():
    values = parse_supported_arguments("Arguments: optimized+ quiet+ cool")
    assert values == {"optimized": True, "quiet": True, "cool": False}


def test_parse_supported_arguments_without_arguments_line():
    assert parse_supported_arguments("Usage: cctk --ThermalManagement=<value>") == {}
    assert parse_supported_arguments("") == {}


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------
def test_probe_reports_only_supported_profiles(tmp_path):
    backend, _ = build_cctk(
        tmp_path,
        {
            "-H --ThermalManagement": (0, THERMAL_HELP_PARTIAL),
            "-H --PrimaryBattChargeCfg": (0, CHARGE_HELP_FULL),
            "-H --AdvBatteryChargeCfg": (0, "Arguments: disabled+ | enabled+"),
        },
    )
    caps = backend.probe()
    assert caps.thermal_profiles == (
        ThermalProfile.OPTIMIZED,
        ThermalProfile.QUIET,
        ThermalProfile.ULTRA_PERFORMANCE,
    )
    assert ThermalProfile.COOL not in caps.thermal_profiles
    assert caps.supports_custom_thresholds
    assert caps.supports_advanced_schedule


def test_probe_falls_back_when_output_is_unparseable(tmp_path):
    backend, _ = build_cctk(
        tmp_path,
        {
            "-H --ThermalManagement": (0, "Usage: something totally different"),
            "-H --PrimaryBattChargeCfg": (0, "Usage: something totally different"),
            "-H --AdvBatteryChargeCfg": (0, ""),
        },
    )
    caps = backend.probe()
    # Failing open: never hide a feature just because we could not parse Dell's text.
    assert caps.thermal_profiles == tuple(ThermalProfile)
    assert caps.charge_modes == tuple(ChargeMode)
    assert any("Could not parse" in note for note in caps.notes)
    # The raw text is kept so a user can report it and we can fix the parser.
    assert any("help said" in note for note in caps.notes)


def test_probe_falls_back_when_nothing_marked_supported(tmp_path):
    """If we misread the format, all values would look unsupported.

    Reporting zero capabilities would silently remove working features from the
    UI, so we must fall back to the documented set instead.
    """
    backend, _ = build_cctk(
        tmp_path,
        {
            "-H --ThermalManagement": (0, "Arguments: optimized quiet cool ultraperformance"),
            "-H --PrimaryBattChargeCfg": (0, "Arguments: standard express primacuse"),
            "-H --AdvBatteryChargeCfg": (0, ""),
        },
    )
    caps = backend.probe()
    assert caps.thermal_profiles == tuple(ThermalProfile)
    assert any("marks none as supported" in note for note in caps.notes)


def test_probe_without_cctk_reports_unavailable(tmp_path):
    backend, _ = build_cctk(tmp_path, {})
    backend.path = None
    caps = backend.probe()
    assert caps.thermal_profiles == ()
    assert caps.control_backend == "cctk"
    assert any("not found" in note for note in caps.notes)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def test_read_profile(tmp_path):
    backend, _ = build_cctk(tmp_path, {"--ThermalManagement": (0, "ThermalManagement=Quiet")})
    assert backend.read_profile() is ThermalProfile.QUIET


def test_read_charge_config(tmp_path):
    backend, _ = build_cctk(
        tmp_path, {"--PrimaryBattChargeCfg": (0, "PrimaryBattChargeCfg=Custom:75-80")}
    )
    assert backend.read_charge_config() == ChargeConfig(
        mode=ChargeMode.CUSTOM, start=75, stop=80
    )


# ---------------------------------------------------------------------------
# Exit-code mapping. These are the difference between "you need a password" and
# "your model cannot do this".
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "code", [C.CCTK_EXIT_SETUP_PWD_REQUIRED, C.CCTK_EXIT_SYS_PWD_REQUIRED]
)
def test_password_required_codes(tmp_path, code):
    backend, _ = build_cctk(tmp_path, {"--ThermalManagement": (code, "Password required")})
    with pytest.raises(BiosPasswordRequired) as excinfo:
        backend.set_profile(ThermalProfile.QUIET)
    assert excinfo.value.exit_code == code


@pytest.mark.parametrize(
    "code", [C.CCTK_EXIT_SETUP_PWD_INVALID, C.CCTK_EXIT_SYS_PWD_INVALID]
)
def test_password_invalid_codes(tmp_path, code):
    backend, _ = build_cctk(tmp_path, {"--ThermalManagement": (code, "Bad password")})
    with pytest.raises(BiosPasswordInvalid):
        backend.set_profile(ThermalProfile.QUIET)


def test_owner_password_code(tmp_path):
    backend, _ = build_cctk(
        tmp_path, {"--PrimaryBattChargeCfg": (C.CCTK_EXIT_OWNER_PWD_SET, "Owner password set")}
    )
    with pytest.raises(BiosPasswordRequired):
        backend.set_charge_config(ChargeConfig(mode=ChargeMode.STANDARD))


def test_unsupported_attribute_detected_from_message(tmp_path):
    backend, _ = build_cctk(
        tmp_path, {"--ThermalManagement": (1, "This system does not support this feature")}
    )
    with pytest.raises(AttributeNotSupported):
        backend.set_profile(ThermalProfile.QUIET)


def test_generic_failure_raises_cctk_error_with_exit_code(tmp_path):
    backend, _ = build_cctk(tmp_path, {"--ThermalManagement": (7, "Something odd happened")})
    with pytest.raises(CctkError) as excinfo:
        backend.set_profile(ThermalProfile.QUIET)
    assert excinfo.value.exit_code == 7
    assert "Something odd happened" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------
def test_set_profile_sends_exact_token(tmp_path):
    backend, runner = build_cctk(tmp_path, {})
    backend.set_profile(ThermalProfile.ULTRA_PERFORMANCE)
    assert any("--ThermalManagement=UltraPerformance" in call for call in runner.wrote)


def test_set_charge_sends_custom_token(tmp_path):
    backend, runner = build_cctk(tmp_path, {})
    backend.set_charge_config(ChargeConfig(mode=ChargeMode.CUSTOM, start=60, stop=80))
    assert any("--PrimaryBattChargeCfg=Custom:60-80" in call for call in runner.wrote)


def test_bios_password_is_passed_as_separate_argv_entries(tmp_path):
    """The password must never be spliced into a shell string."""
    backend, runner = build_cctk(tmp_path, {}, bios_password="hunter2")
    backend.set_profile(ThermalProfile.QUIET)
    argv = runner.calls[-1]
    assert f"{C.BIOS_PWD_ARG_SETUP}=hunter2" in argv
    assert f"{C.BIOS_PWD_ARG_SYS}=hunter2" in argv


def test_missing_executable_raises_backend_unavailable(tmp_path):
    backend, _ = build_cctk(tmp_path, {})
    backend.path = tmp_path / "definitely-not-here.exe"
    with pytest.raises(BackendUnavailableError):
        backend.set_profile(ThermalProfile.QUIET)


def test_is_elevated_on_posix_matches_euid():
    """On POSIX, elevation means uid 0; Windows takes a different code path."""
    import os

    if sys.platform == "win32":
        pytest.skip("os.geteuid() does not exist on Windows")
    backend = object.__new__(__import__(
        "xpshelper.backends.cctk", fromlist=["CctkBackend"]
    ).CctkBackend)
    assert backend.is_elevated() == (os.geteuid() == 0)
