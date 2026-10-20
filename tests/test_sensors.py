"""Tests for the Intel DPTF temperature source.

Reverse-engineered on an XPS 13 7390 2-in-1, where the obvious sources are all
dead ends:

* ``MSAcpi_ThermalZoneTemperature`` answers **Not supported**.
* The SMBIOS temperature probes (``Win32_TemperatureProbe``: one CPU, two
  DIMM A, three SKIN) exist but **never carry a reading** -- SMBIOS type 28
  describes a probe and has no current-value field, which is exactly why
  ``CurrentReading`` is null there.
* Dell Command | Monitor is not installed, so ``root/dcim/sysman`` has no
  ``DCIM_NumericSensor``.

What does work is ``root\\wmi :: EsifDeviceInformation``, published by the
"Intel(R) Dynamic Tuning Processor Participant" device: one entry per DPTF thermal
zone, with a temperature, a per-zone power, and trip points. It returns data
**only when elevated** -- unelevated it yields an empty set rather than an error,
which is what made the sensors look absent.
"""

from __future__ import annotations

import pytest

from xpshelper.backends.wmi import RawTelemetry, temperatures_from_telemetry
from xpshelper.model import TemperatureReading


def _zone(temperature, power=0):
    return {
        "InstanceName": "PCI\\VEN_8086&DEV_8A03&SUBSYS_08B01028&REV_03\\3&11583659&0&20_0",
        "Temperature": temperature,
        "Power": power,
        "Ac0TripPoint": 67,
        "Ac1TripPoint": 55,
        "CrtTripPoint": 0,
        "Active": True,
    }


def test_dptf_zones_are_read_when_present():
    raw = RawTelemetry(esif_zones=[_zone(35), _zone(27), _zone(54, power=131)])
    readings = temperatures_from_telemetry(raw)
    assert len(readings) == 3
    assert all(r.source == "wmi:dptf" for r in readings)


def test_readings_are_sorted_hottest_first():
    """The first reading is what the UI shows as "the" temperature."""
    raw = RawTelemetry(esif_zones=[_zone(27), _zone(54, power=131), _zone(35)])
    readings = temperatures_from_telemetry(raw)
    assert [r.celsius for r in readings] == [54, 35, 27]


def test_zero_degree_zones_are_dropped():
    """0 C is the firmware's unused-zone sentinel, not a reading.

    This machine reports nine zones of which one is always 0; showing it would
    put a false 0 C sensor in front of the user.
    """
    raw = RawTelemetry(esif_zones=[_zone(0), _zone(35), _zone(0), _zone(50)])
    readings = temperatures_from_telemetry(raw)
    assert [r.celsius for r in readings] == [50, 35]


def test_power_appears_in_the_label_when_reported():
    """Power domains identify themselves; the rest are unnamed by the firmware."""
    raw = RawTelemetry(esif_zones=[_zone(54, power=131), _zone(35)])
    readings = temperatures_from_telemetry(raw)
    assert "131 mW" in readings[0].name
    assert "mW" not in readings[1].name


def test_zones_are_labelled_by_index_not_invented_names():
    """The firmware gives an index, not "CPU" or "SKIN".

    Guessing a name would be worse than an honest index: the DPTF participant
    names live in the policy configuration, which is not exposed over WMI.
    """
    raw = RawTelemetry(esif_zones=[_zone(35), _zone(40)])
    readings = temperatures_from_telemetry(raw)
    assert readings[0].name == "DPTF zone 1"
    assert readings[1].name == "DPTF zone 0"


def test_dptf_wins_over_the_acpi_fallback():
    raw = RawTelemetry(
        esif_zones=[_zone(54)],
        acpi_thermal=[{"InstanceName": "x", "CurrentTemperature": 3000}],
    )
    readings = temperatures_from_telemetry(raw)
    assert [r.source for r in readings] == ["wmi:dptf"]


def test_acpi_still_works_when_dptf_is_absent():
    """Older machines have no DPTF; the ACPI path must keep working."""
    raw = RawTelemetry(esif_zones=[], acpi_thermal=[{"CurrentTemperature": 3132}])
    readings = temperatures_from_telemetry(raw)
    assert len(readings) == 1
    assert readings[0].source == "wmi:acpi"
    assert readings[0].celsius == pytest.approx(3132 / 10.0 - 273.15, abs=0.01)


def test_no_sources_yields_no_readings_rather_than_an_error():
    readings = temperatures_from_telemetry(RawTelemetry())
    assert readings == []


def test_a_missing_temperature_field_is_skipped():
    raw = RawTelemetry(esif_zones=[{"InstanceName": "x", "Power": 5}, _zone(35)])
    readings = temperatures_from_telemetry(raw)
    assert len(readings) == 1


def test_the_probe_script_asks_for_the_dptf_class():
    """The query must exist, or the source silently disappears again."""
    from xpshelper.backends.wmi import TELEMETRY_SCRIPT

    assert "EsifDeviceInformation" in TELEMETRY_SCRIPT
    assert "esif_zones" in TELEMETRY_SCRIPT


def test_the_probe_script_still_asks_for_the_older_sources():
    """DPTF is the one that works here, not the only one supported."""
    from xpshelper.backends.wmi import TELEMETRY_SCRIPT

    for source in ("MSAcpi_ThermalZoneTemperature", "DCIM_NumericSensor", "LibreHardwareMonitor"):
        assert source in TELEMETRY_SCRIPT, source


def test_temperature_readings_carry_their_source():
    reading = TemperatureReading(name="DPTF zone 0", celsius=35.0, source="wmi:dptf")
    assert reading.source == "wmi:dptf"
