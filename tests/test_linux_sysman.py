"""Tests for the Linux firmware-attributes backend and the composite selector.

The sysfs tree is faked on disk, so the real read/write logic is exercised
without any Dell hardware or kernel module.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from xpshelper.backends.base import NullControlBackend
from xpshelper.backends.composite import CompositeControlBackend
from xpshelper.backends.linux_sysman import (
    LinuxSysmanBackend,
    discover_attributes,
    parse_possible_values,
)
from xpshelper.backends.mock import MockBackend
from xpshelper.errors import AttributeNotSupported, BiosPasswordRequired, XpsHelperError
from xpshelper.model import Capabilities, ChargeConfig, ChargeMode, ThermalProfile


def build_sysfs(
    root: Path,
    *,
    provider: str = "dell-wmi-sysman",
    thermal_current: str = "Optimized",
    thermal_values: str = "Optimized Quiet Cool UltraPerformance",
    charge_current: str | None = "Adaptive",
    charge_values: str = "Standard Express PrimAcUse Adaptive Custom",
    with_adv: bool = False,
) -> Path:
    """Create a directory tree shaped like the real firmware-attributes class."""
    base = root / provider / "attributes"

    thermal = base / "ThermalManagement"
    thermal.mkdir(parents=True)
    (thermal / "current_value").write_text(thermal_current, encoding="utf-8")
    (thermal / "possible_values").write_text(thermal_values, encoding="utf-8")
    (thermal / "type").write_text("enumeration", encoding="utf-8")

    if charge_current is not None:
        charge = base / "PrimaryBattChargeCfg"
        charge.mkdir(parents=True)
        (charge / "current_value").write_text(charge_current, encoding="utf-8")
        (charge / "possible_values").write_text(charge_values, encoding="utf-8")

    if with_adv:
        adv = base / "AdvBatteryChargeCfg"
        adv.mkdir(parents=True)
        (adv / "current_value").write_text("Disabled", encoding="utf-8")

    # The provider-level authentication file used when a BIOS password is set.
    (root / provider / "authentication").write_text("", encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# Discovery and parsing
# ---------------------------------------------------------------------------
def test_discover_attributes(tmp_path):
    build_sysfs(tmp_path)
    found = discover_attributes(tmp_path)
    assert set(found) == {"ThermalManagement", "PrimaryBattChargeCfg"}
    assert found["ThermalManagement"].name == "ThermalManagement"


def test_discover_attributes_ignores_dirs_without_current_value(tmp_path):
    stray = tmp_path / "provider" / "attributes" / "NotAnAttribute"
    stray.mkdir(parents=True)
    assert discover_attributes(tmp_path) == {}


def test_discover_attributes_on_missing_root(tmp_path):
    assert discover_attributes(tmp_path / "nope") == {}


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Optimized Quiet Cool", ["Optimized", "Quiet", "Cool"]),
        ("Optimized,Quiet,Cool", ["Optimized", "Quiet", "Cool"]),
        ('"Primarily AC Use" Adaptive', ["Primarily AC Use", "Adaptive"]),
        ("", []),
        (None, []),
    ],
)
def test_parse_possible_values(raw, expected):
    assert parse_possible_values(raw) == expected


# ---------------------------------------------------------------------------
# Backend reads / probe
# ---------------------------------------------------------------------------
def test_backend_unavailable_without_attributes(tmp_path):
    backend = LinuxSysmanBackend(tmp_path)
    assert backend.available is False
    caps = backend.probe()
    assert caps.thermal_profiles == ()
    assert any("No BIOS attributes found" in note for note in caps.notes)


def test_probe_reports_supported_values(tmp_path):
    build_sysfs(tmp_path)
    caps = LinuxSysmanBackend(tmp_path).probe()

    assert caps.thermal_profiles == tuple(ThermalProfile)
    assert caps.charge_modes == tuple(ChargeMode)
    assert any("ThermalManagement" in note for note in caps.notes)


def test_probe_handles_missing_possible_values(tmp_path):
    build_sysfs(tmp_path, thermal_values="")
    caps = LinuxSysmanBackend(tmp_path).probe()
    # Fail open rather than hiding the feature.
    assert caps.thermal_profiles == tuple(ThermalProfile)
    assert any("no possible_values" in note for note in caps.notes)


def test_probe_detects_advanced_schedule(tmp_path):
    build_sysfs(tmp_path, with_adv=True)
    assert LinuxSysmanBackend(tmp_path).probe().supports_advanced_schedule is True


def test_read_profile_and_charge(tmp_path):
    build_sysfs(tmp_path, thermal_current="Quiet", charge_current="Custom:60-80")
    backend = LinuxSysmanBackend(tmp_path)
    assert backend.read_profile() is ThermalProfile.QUIET
    assert backend.read_charge_config() == ChargeConfig(
        mode=ChargeMode.CUSTOM, start=60, stop=80
    )


def test_read_returns_none_when_attribute_absent(tmp_path):
    build_sysfs(tmp_path, charge_current=None)
    backend = LinuxSysmanBackend(tmp_path)
    assert backend.read_profile() is ThermalProfile.OPTIMIZED
    assert backend.read_charge_config() is None


# ---------------------------------------------------------------------------
# Backend writes
# ---------------------------------------------------------------------------
def test_set_profile_writes_exact_token(tmp_path):
    build_sysfs(tmp_path)
    backend = LinuxSysmanBackend(tmp_path)
    backend.set_profile(ThermalProfile.ULTRA_PERFORMANCE)

    written = (tmp_path / "dell-wmi-sysman" / "attributes" / "ThermalManagement"
               / "current_value").read_text(encoding="utf-8")
    assert written == "UltraPerformance"
    assert backend.read_profile() is ThermalProfile.ULTRA_PERFORMANCE


def test_set_charge_writes_custom_token(tmp_path):
    build_sysfs(tmp_path)
    backend = LinuxSysmanBackend(tmp_path)
    backend.set_charge_config(ChargeConfig(mode=ChargeMode.CUSTOM, start=60, stop=80))
    written = (tmp_path / "dell-wmi-sysman" / "attributes" / "PrimaryBattChargeCfg"
               / "current_value").read_text(encoding="utf-8")
    assert written == "Custom:60-80"


def test_set_profile_on_missing_attribute_raises(tmp_path):
    build_sysfs(tmp_path, charge_current=None)
    # Remove the thermal attribute entirely.
    import shutil

    shutil.rmtree(tmp_path / "dell-wmi-sysman" / "attributes" / "ThermalManagement")
    with pytest.raises(AttributeNotSupported):
        LinuxSysmanBackend(tmp_path).set_profile(ThermalProfile.QUIET)


def test_permission_error_becomes_password_required(tmp_path, monkeypatch):
    build_sysfs(tmp_path)
    backend = LinuxSysmanBackend(tmp_path)  # no password configured

    real_write = Path.write_text

    def deny(self, *args, **kwargs):
        if self.name == "current_value":
            raise PermissionError(13, "Permission denied")
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", deny)
    with pytest.raises(BiosPasswordRequired):
        backend.set_profile(ThermalProfile.QUIET)


def test_password_is_used_to_authenticate_then_retry(tmp_path, monkeypatch):
    """With a BIOS password set, the provider's authentication file is written first."""
    build_sysfs(tmp_path)
    backend = LinuxSysmanBackend(tmp_path, bios_password="hunter2")

    real_write = Path.write_text
    state = {"denied_once": False}

    def flaky(self, *args, **kwargs):
        if self.name == "current_value" and not state["denied_once"]:
            state["denied_once"] = True
            raise PermissionError(13, "Permission denied")
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", flaky)
    backend.set_profile(ThermalProfile.QUIET)

    auth = (tmp_path / "dell-wmi-sysman" / "authentication").read_text(encoding="utf-8")
    assert auth == "hunter2"
    assert backend.read_profile() is ThermalProfile.QUIET


def test_invalid_value_errno_maps_to_unsupported(tmp_path, monkeypatch):
    build_sysfs(tmp_path)
    backend = LinuxSysmanBackend(tmp_path)

    real_write = Path.write_text

    def fail(self, *args, **kwargs):
        if self.name == "current_value":
            raise OSError(22, "Invalid argument")
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail)
    with pytest.raises(AttributeNotSupported):
        backend.set_profile(ThermalProfile.QUIET)


# ---------------------------------------------------------------------------
# Composite selection
# ---------------------------------------------------------------------------
class ReadOnlyControl:
    """A backend that can read but reports no controllable settings."""

    name = "read-only"

    def probe(self) -> Capabilities:
        caps = Capabilities(control_backend=self.name)
        caps.notes.append("nothing controllable here")
        return caps

    def read_profile(self):
        return None

    def read_charge_config(self):
        return None

    def set_profile(self, profile):
        raise NotImplementedError

    def set_charge_config(self, config):
        raise NotImplementedError


def test_composite_selects_first_backend_that_can_control():
    read_only = ReadOnlyControl()
    workable = MockBackend()
    composite = CompositeControlBackend([read_only, workable])

    assert composite.selected() is workable
    assert composite.name == "mock"
    assert composite.probe().can_control


def test_composite_skips_backends_whose_probe_raises():
    class Broken:
        name = "broken"

        def probe(self):
            raise RuntimeError("boom")

    composite = CompositeControlBackend([Broken(), MockBackend()])
    assert composite.selected().name == "mock"


def test_composite_falls_back_to_read_only_and_explains():
    composite = CompositeControlBackend([ReadOnlyControl(), NullControlBackend("no cctk")])
    caps = composite.probe()

    assert caps.can_control is False
    # The reasons from every candidate must be preserved for `doctor`.
    notes = " ".join(caps.notes)
    assert "nothing controllable here" in notes
    assert "no cctk" in notes


def test_composite_write_without_control_raises_with_reasons():
    composite = CompositeControlBackend([ReadOnlyControl()])
    with pytest.raises(XpsHelperError) as excinfo:
        composite.set_profile(ThermalProfile.QUIET)
    assert "nothing controllable here" in str(excinfo.value)


def test_composite_refresh_clears_selection():
    composite = CompositeControlBackend([MockBackend()])
    assert composite.selected() is not None
    composite.refresh()
    assert composite._capabilities is None
    assert composite._selected is None
