"""Tests for third-party sensor providers.

Context that matters: this machine's firmware exposes no wattages, and reading
package power or the PL1/PL2 limits means executing RDMSR, which is a ring-0
instruction. Every tool that shows those numbers -- HWMonitor, HWiNFO,
LibreHardwareMonitor -- ships a kernel driver to do it. This project does not, so
it reads from a provider the user is already running instead.

HWMonitor publishes no interface at all. LibreHardwareMonitor and
OpenHardwareMonitor publish WMI classes; HWiNFO publishes a registry report.
"""

from __future__ import annotations

import sys

import pytest

from xpshelper import vendorsensors as vs


def _lhm(name, sensor_type, value, parent=""):
    return {"Name": name, "SensorType": sensor_type, "Value": value, "Parent": parent}


# ---------------------------------------------------------------------------
# WMI providers (LibreHardwareMonitor / OpenHardwareMonitor)
# ---------------------------------------------------------------------------
def test_lhm_temperature_rows_are_normalised():
    sensors = vs.sensors_from_wmi_entries(
        [_lhm("CPU Package", "Temperature", 51.0, "/intelcpu/0")], "lhm"
    )
    assert len(sensors) == 1
    assert sensors[0].kind == vs.KIND_TEMPERATURE
    assert sensors[0].value == 51.0
    assert sensors[0].unit == "C"
    assert sensors[0].provider == "lhm"


def test_parent_is_folded_into_the_label():
    sensors = vs.sensors_from_wmi_entries(
        [_lhm("Package", "Temperature", 51.0, "/intelcpu/0")], "lhm"
    )
    assert sensors[0].name == "/intelcpu/0: Package"


def test_ohm_rows_use_the_same_shape_as_lhm():
    """OpenHardwareMonitor publishes an identical Sensor class."""
    sensors = vs.sensors_from_wmi_entries(
        [_lhm("CPU Package", "Power", 2.91, "/intelcpu/0")], "ohm"
    )
    assert sensors[0].kind == vs.KIND_POWER
    assert sensors[0].unit == "W"
    assert sensors[0].provider == "ohm"


def test_types_we_do_not_report_are_dropped():
    rows = [
        _lhm("CPU Total", "Load", 9.8),
        _lhm("Cores", "Clock", 3491.5),
        _lhm("Package", "Temperature", 51.0),
    ]
    sensors = vs.sensors_from_wmi_entries(rows, "lhm")
    assert [s.kind for s in sensors] == [vs.KIND_TEMPERATURE]


def test_a_missing_value_is_skipped_not_coerced_to_zero():
    """A zero we invented would be indistinguishable from a real 0 C."""
    rows = [_lhm("Broken", "Temperature", None), _lhm("Good", "Temperature", 44.0)]
    sensors = vs.sensors_from_wmi_entries(rows, "lhm")
    assert [s.value for s in sensors] == [44.0]


def test_non_dict_rows_are_ignored():
    assert vs.sensors_from_wmi_entries([None, "x", 3], "lhm") == []


def test_empty_input_yields_nothing():
    assert vs.sensors_from_wmi_entries([], "lhm") == []


# ---------------------------------------------------------------------------
# HWiNFO registry
# ---------------------------------------------------------------------------
class _FakeKey:
    def __init__(self, values):
        self._values = values

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_winreg(values_by_path):
    """A stand-in for winreg that serves one registry key."""
    class _W:
        HKEY_LOCAL_MACHINE = object()

        @staticmethod
        def OpenKey(root, path):
            if path in values_by_path:
                return _FakeKey(values_by_path[path])
            raise OSError("not found")

        @staticmethod
        def EnumValue(key, index):
            if index >= len(key._values):
                raise OSError("no more")
            return key._values[index]

    return _W


def test_hwinfo_registry_rows_are_parsed(monkeypatch):
    """Layout: Sensor<N> label, Value<N> value, ValueType<N> kind (5 = power)."""
    values = [
        ("Sensor0", "CPU Package", 1),
        ("Value0", "2.91", 1),
        ("ValueType0", 5, 4),
        ("Sensor1", "CPU Package", 1),
        ("Value1", "51.0", 1),
        ("ValueType1", 1, 4),
    ]
    fake = _fake_winreg({vs.HWINFO_KEYS[0]: values})
    monkeypatch.setitem(sys.modules, "winreg", fake)

    sensors = vs.hwinfo_sensors()
    by_kind = {s.kind: s for s in sensors}
    assert by_kind[vs.KIND_POWER].value == 2.91
    assert by_kind[vs.KIND_TEMPERATURE].value == 51.0
    assert all(s.provider == "hwinfo" for s in sensors)


def test_hwinfo_accepts_the_label_spelling(monkeypatch):
    """Some builds name the label value Label<N> instead of Sensor<N>."""
    values = [
        ("Label0", "CPU Package", 1),
        ("Value0", "47.5", 1),
        ("ValueType0", 1, 4),
    ]
    monkeypatch.setitem(sys.modules, "winreg", _fake_winreg({vs.HWINFO_KEYS[0]: values}))
    sensors = vs.hwinfo_sensors()
    assert len(sensors) == 1
    assert sensors[0].value == 47.5


def test_hwinfo_absent_returns_nothing(monkeypatch):
    monkeypatch.setitem(sys.modules, "winreg", _fake_winreg({}))
    assert vs.hwinfo_sensors() == []


def test_hwinfo_with_unknown_shape_fails_closed(monkeypatch):
    """An unrecognised layout must yield nothing, never a guessed reading.

    This is the important one: the registry is written by software we do not
    control, so a number that might be the wrong sensor is worse than a blank.
    """
    values = [("SomethingElse", "51.0", 1), ("AnotherThing", "2.91", 1)]
    monkeypatch.setitem(sys.modules, "winreg", _fake_winreg({vs.HWINFO_KEYS[0]: values}))
    assert vs.hwinfo_sensors() == []


def test_hwinfo_rows_without_a_label_are_dropped(monkeypatch):
    values = [("Value0", "51.0", 1), ("ValueType0", 1, 4)]
    monkeypatch.setitem(sys.modules, "winreg", _fake_winreg({vs.HWINFO_KEYS[0]: values}))
    assert vs.hwinfo_sensors() == []


def test_hwinfo_unknown_value_type_is_dropped(monkeypatch):
    values = [
        ("Sensor0", "Something", 1),
        ("Value0", "7", 1),
        ("ValueType0", 99, 4),
    ]
    monkeypatch.setitem(sys.modules, "winreg", _fake_winreg({vs.HWINFO_KEYS[0]: values}))
    assert vs.hwinfo_sensors() == []


def test_hwinfo_off_windows_returns_nothing(monkeypatch):
    import sys as _sys

    monkeypatch.setattr(_sys, "platform", "linux")
    assert vs.hwinfo_sensors() == []


# ---------------------------------------------------------------------------
# Interpretation
# ---------------------------------------------------------------------------
def _sensor(name, value, kind):
    return vs.VendorSensor(name=name, value=value, kind=kind, provider="lhm")


def test_package_temperature_is_found_by_name():
    sensors = [
        _sensor("Core #1", 48.0, vs.KIND_TEMPERATURE),
        _sensor("CPU Package", 51.0, vs.KIND_TEMPERATURE),
    ]
    assert vs.package_temperature(sensors).value == 51.0


def test_package_temperature_absent_returns_none():
    assert vs.package_temperature([_sensor("Core #1", 48.0, vs.KIND_TEMPERATURE)]) is None


def test_package_power_is_found_by_name():
    sensors = [
        _sensor("IA Cores", 1.48, vs.KIND_POWER),
        _sensor("CPU Package", 2.91, vs.KIND_POWER),
    ]
    assert vs.package_power(sensors).value == 2.91


def test_pl1_and_pl2_are_found():
    """These are the MSRs that would show a thermal mode changing a power limit."""
    sensors = [
        _sensor("Power Max (PL1)", 46.0, vs.KIND_POWER),
        _sensor("Short Power Max (PL2)", 46.0, vs.KIND_POWER),
    ]
    assert vs.power_limit(sensors, "1").value == 46.0
    assert vs.power_limit(sensors, "2").value == 46.0


def test_pl_lookup_does_not_confuse_pl1_with_pl2():
    sensors = [_sensor("Power Max (PL1)", 15.0, vs.KIND_POWER)]
    assert vs.power_limit(sensors, "2") is None


def test_power_limit_absent_returns_none():
    assert vs.power_limit([], "1") is None
