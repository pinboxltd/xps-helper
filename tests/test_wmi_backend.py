"""WMI telemetry parsing tests.

The PowerShell call itself cannot run here, so the JSON it produces is canned
and the interpretation layer is verified thoroughly. That is the part that can
be wrong in ways a user would notice (a battery shown as discharging while
plugged in, temperatures off by 273 degrees, and so on).
"""

from __future__ import annotations

import json
import sys

import pytest

from xpshelper.backends.wmi import (
    WmiTelemetryBackend,
    battery_from_telemetry,
    fans_from_telemetry,
    parse_telemetry_json,
    temperatures_from_telemetry,
)


def make(**sections) -> object:
    """Build RawTelemetry from keyword sections, as PowerShell would emit."""
    return parse_telemetry_json(json.dumps(sections))


# ---------------------------------------------------------------------------
# JSON decoding
# ---------------------------------------------------------------------------
def test_parse_full_document():
    raw = make(
        battery=[{"EstimatedChargeRemaining": 76, "BatteryStatus": 2}],
        dcim_sensors=[{"ElementName": "Fan 1", "CurrentReading": 2400}],
    )
    assert raw.battery[0]["EstimatedChargeRemaining"] == 76
    assert raw.dcim_sensors[0]["CurrentReading"] == 2400


def test_parse_normalises_single_object_to_list():
    """PowerShell emits an object, not an array, when there is one result."""
    payload = json.dumps({"battery": {"EstimatedChargeRemaining": 50, "BatteryStatus": 1}})
    raw = parse_telemetry_json(payload)
    assert len(raw.battery) == 1
    assert raw.battery[0]["EstimatedChargeRemaining"] == 50


def test_parse_rejects_garbage_without_raising():
    for payload in ("", "   ", "not json", json.dumps([1, 2, 3]), "null"):
        raw = parse_telemetry_json(payload)
        assert raw.battery == []
        assert raw.dcim_sensors == []


def test_parse_handles_null_sections():
    raw = parse_telemetry_json(json.dumps({"battery": None, "dcim_sensors": None}))
    assert raw.battery == []


# ---------------------------------------------------------------------------
# Battery
# ---------------------------------------------------------------------------
def test_battery_discharging_on_battery():
    battery = battery_from_telemetry(
        make(battery=[{"EstimatedChargeRemaining": 42, "BatteryStatus": 1}])
    )
    assert battery.percent == 42
    assert battery.plugged_in is False
    assert battery.charging is False


def test_battery_plugged_in_status_2():
    battery = battery_from_telemetry(
        make(battery=[{"EstimatedChargeRemaining": 80, "BatteryStatus": 2}])
    )
    assert battery.plugged_in is True
    assert battery.charging is False  # full / not actively charging


def test_battery_actively_charging_status_6():
    battery = battery_from_telemetry(
        make(battery=[{"EstimatedChargeRemaining": 55, "BatteryStatus": 6}])
    )
    assert battery.plugged_in is True
    assert battery.charging is True


def test_battery_health_from_root_wmi_when_win32_reports_zero():
    """Dell commonly reports 0 in Win32_Battery; root/wmi is authoritative."""
    battery = battery_from_telemetry(
        make(
            battery=[{"EstimatedChargeRemaining": 70, "BatteryStatus": 2,
                      "DesignCapacity": 0, "FullChargeCapacity": 0}],
            battery_static=[{"DesignedCapacity": 52000}],
            battery_full=[{"FullChargedCapacity": 47500}],
        )
    )
    assert battery.design_capacity_mwh == 52000
    assert battery.full_capacity_mwh == 47500
    assert battery.health_percent == 91.3


def test_battery_health_not_computed_without_design_capacity():
    battery = battery_from_telemetry(
        make(battery=[{"EstimatedChargeRemaining": 70, "BatteryStatus": 2}])
    )
    assert battery is not None
    assert battery.health_percent is None


def test_battery_absent_returns_none():
    assert battery_from_telemetry(make()) is None


def test_battery_empty_sections_return_none():
    assert battery_from_telemetry(make(battery=[], battery_static=[], battery_full=[])) is None


# ---------------------------------------------------------------------------
# Temperatures
# ---------------------------------------------------------------------------
def test_libre_hardware_monitor_preferred():
    temps = temperatures_from_telemetry(
        make(
            lhm_sensors=[
                {"Name": "CPU Package", "SensorType": "Temperature", "Value": 61.5},
                {"Name": "GPU Core", "SensorType": "Temperature", "Value": 45.0},
                {"Name": "CPU Total", "SensorType": "Load", "Value": 12.0},
            ],
            dcim_sensors=[{"ElementName": "CPU Temp", "CurrentReading": 99.0}],
        )
    )
    names = [t.name for t in temps]
    assert names == ["CPU Package", "GPU Core"]
    assert temps[0].celsius == 61.5
    assert temps[0].source == "wmi:lhm"


def test_lhm_average_and_max_are_not_used_as_primary():
    temps = temperatures_from_telemetry(
        make(lhm_sensors=[
            {"Name": "CPU Core Average", "SensorType": "Temperature", "Value": 50.0},
            {"Name": "CPU Core Max", "SensorType": "Temperature", "Value": 55.0},
        ])
    )
    assert temps == []


def test_dcim_temperature_used_when_lhm_absent():
    temps = temperatures_from_telemetry(
        make(dcim_sensors=[
            {"ElementName": "CPU Temperature", "CurrentReading": 58.0, "BaseUnits": "Degrees C"},
            {"ElementName": "Fan 1", "CurrentReading": 2400, "BaseUnits": "RPM"},
        ])
    )
    assert [t.name for t in temps] == ["CPU Temperature"]
    assert temps[0].source == "wmi:dcim"


def test_acpi_thermal_zone_kelvin_conversion():
    """MSAcpi_ThermalZoneTemperature reports tenths of a degree Kelvin."""
    temps = temperatures_from_telemetry(make(acpi_thermal=[{"CurrentTemperature": 2982}]))
    assert len(temps) == 1
    assert temps[0].celsius == 2982 / 10.0 - 273.15
    assert temps[0].source == "wmi:acpi"


def test_acpi_nonsense_readings_are_discarded():
    # A raw 0 would convert to -273 C and must not be shown to the user.
    assert temperatures_from_telemetry(make(acpi_thermal=[{"CurrentTemperature": 0}])) == []


def test_no_sensors_returns_empty():
    assert temperatures_from_telemetry(make()) == []


# ---------------------------------------------------------------------------
# Fans
# ---------------------------------------------------------------------------
def test_fans_from_dcim_by_element_name():
    fans = fans_from_telemetry(
        make(dcim_sensors=[
            {"ElementName": "Fan 1", "CurrentReading": 2413, "BaseUnits": "RPM"},
            {"ElementName": "Fan 2", "CurrentReading": 0, "BaseUnits": "RPM"},
            {"ElementName": "CPU Temp", "CurrentReading": 58.0, "BaseUnits": "Degrees C"},
        ])
    )
    assert [f.name for f in fans] == ["Fan 1", "Fan 2"]
    assert fans[0].rpm == 2413


def test_fans_detected_by_unit_when_name_is_odd():
    fans = fans_from_telemetry(
        make(dcim_sensors=[{"ElementName": "System Board Fan", "CurrentReading": 1800,
                            "BaseUnits": "Revolutions per minute"}])
    )
    # "System Board Fan" does not start with "fan", but the unit gives it away.
    assert len(fans) == 1


def test_fans_skip_missing_readings():
    fans = fans_from_telemetry(
        make(dcim_sensors=[{"ElementName": "Fan 1", "CurrentReading": None}])
    )
    assert fans == []


def test_fans_absent_without_dcim():
    assert fans_from_telemetry(make()) == []


# ---------------------------------------------------------------------------
# Backend guardrails
# ---------------------------------------------------------------------------
def test_backend_reports_unavailable_off_windows():
    if sys.platform == "win32":
        pytest.skip("this assertion is about non-Windows platforms")
    backend = WmiTelemetryBackend()
    assert backend.available is False
    assert backend.read_battery() is None
    assert backend.read_temperatures() == []
    assert backend.read_fans() == []
    assert any("not running on Windows" in note for note in backend.describe())


def test_backend_caches_snapshot_until_invalidated():
    backend = WmiTelemetryBackend()
    backend._cache = parse_telemetry_json(
        json.dumps({"battery": [{"EstimatedChargeRemaining": 10, "BatteryStatus": 1}]})
    )
    assert backend.read_battery().percent == 10
    backend.invalidate()
    assert backend._cache is None
