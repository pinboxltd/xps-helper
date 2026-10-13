"""Windows telemetry backend (battery, temperatures, fan speeds) via WMI.

Design choice
-------------
All WMI reads happen in **one** PowerShell invocation that emits a single JSON
document. Spawning PowerShell costs a few hundred milliseconds, and a naive
implementation would do that once per sensor class per refresh; batching keeps a
full status refresh to one spawn.

This also means there is no ``pywin32`` dependency: stock Python plus the
PowerShell that ships with Windows is enough. The parsing is pure and unit
tested against canned JSON, because that is the part that can be verified
off-hardware.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..model import BatteryStatus, FanReading, TemperatureReading
from ..procutil import hidden_process_kwargs, powershell_argv

log = logging.getLogger(__name__)

#: Every section is wrapped in its own try/catch so one missing namespace
#: (e.g. no Dell Command | Monitor, so no root/dcim/sysman) degrades to an empty
#: list instead of failing the whole refresh.
TELEMETRY_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'
$out = [ordered]@{}

$out.battery = @()
try {
  $out.battery = @(Get-CimInstance -Namespace 'root/cimv2' -ClassName 'Win32_Battery' |
    Select-Object Name, EstimatedChargeRemaining, BatteryStatus, DesignCapacity, FullChargeCapacity)
} catch { }

$out.battery_static = @()
try {
  $out.battery_static = @(Get-CimInstance -Namespace 'root/wmi' -ClassName 'BatteryStaticData' |
    Select-Object InstanceName, DesignedCapacity, DeviceName)
} catch { }

$out.battery_full = @()
try {
  $out.battery_full = @(Get-CimInstance -Namespace 'root/wmi' -ClassName 'BatteryFullChargedCapacity' |
    Select-Object InstanceName, FullChargedCapacity)
} catch { }

$out.dcim_sensors = @()
try {
  $out.dcim_sensors = @(Get-CimInstance -Namespace 'root/dcim/sysman' -ClassName 'DCIM_NumericSensor' |
    Select-Object DeviceID, ElementName, SensorType, CurrentReading, BaseUnits)
} catch { }

$out.lhm_sensors = @()
try {
  $out.lhm_sensors = @(Get-CimInstance -Namespace 'root/LibreHardwareMonitor' -ClassName 'Sensor' |
    Select-Object Name, SensorType, Value, Identifier, Parent)
} catch { }

$out.acpi_thermal = @()
try {
  $out.acpi_thermal = @(Get-CimInstance -Namespace 'root/wmi' -ClassName 'MSAcpi_ThermalZoneTemperature' |
    Select-Object InstanceName, CurrentTemperature)
} catch { }

$out | ConvertTo-Json -Compress -Depth 4
"""


@dataclass
class RawTelemetry:
    """Decoded JSON sections, before interpretation."""

    battery: list[dict[str, Any]] = field(default_factory=list)
    battery_static: list[dict[str, Any]] = field(default_factory=list)
    battery_full: list[dict[str, Any]] = field(default_factory=list)
    dcim_sensors: list[dict[str, Any]] = field(default_factory=list)
    lhm_sensors: list[dict[str, Any]] = field(default_factory=list)
    acpi_thermal: list[dict[str, Any]] = field(default_factory=list)


def _as_list(value: Any) -> list[dict[str, Any]]:
    """PowerShell emits an object rather than an array for single results."""
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_telemetry_json(payload: str) -> RawTelemetry:
    """Decode the PowerShell JSON document into :class:`RawTelemetry`."""
    payload = (payload or "").strip()
    if not payload:
        return RawTelemetry()
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        log.warning("could not decode WMI telemetry JSON")
        return RawTelemetry()
    if not isinstance(data, dict):
        return RawTelemetry()
    return RawTelemetry(
        battery=_as_list(data.get("battery")),
        battery_static=_as_list(data.get("battery_static")),
        battery_full=_as_list(data.get("battery_full")),
        dcim_sensors=_as_list(data.get("dcim_sensors")),
        lhm_sensors=_as_list(data.get("lhm_sensors")),
        acpi_thermal=_as_list(data.get("acpi_thermal")),
    )


# Win32_Battery.BatteryStatus: 1 = discharging, 2 = on AC, 3-5 = charging states,
# 6-9 = charging, 11 = partially charged. Treat >= 2 as "external power present".
_CHARGING_CODES = {6, 7, 8, 9, 11}


def battery_from_telemetry(raw: RawTelemetry) -> BatteryStatus | None:
    """Interpret the battery sections into a single :class:`BatteryStatus`."""
    if not raw.battery and not raw.battery_static and not raw.battery_full:
        return None

    entry = raw.battery[0] if raw.battery else {}
    percent = _int(entry.get("EstimatedChargeRemaining"))
    status_code = _int(entry.get("BatteryStatus"))
    plugged_in = None if status_code is None else status_code >= 2
    charging = None if status_code is None else status_code in _CHARGING_CODES

    design = _int(entry.get("DesignCapacity"))
    full = _int(entry.get("FullChargeCapacity"))

    # Dell usually reports 0 here; root/wmi is the reliable source.
    if not design and raw.battery_static:
        design = _int(raw.battery_static[0].get("DesignedCapacity"))
    if not full and raw.battery_full:
        full = _int(raw.battery_full[0].get("FullChargedCapacity"))

    health = None
    if design and full and design > 0:
        health = round(full / design * 100, 1)

    return BatteryStatus(
        percent=percent,
        plugged_in=plugged_in,
        charging=charging,
        health_percent=health,
        design_capacity_mwh=design or None,
        full_capacity_mwh=full or None,
        source="wmi",
    )


def _dcim_is_fan(entry: dict[str, Any]) -> bool:
    name = str(entry.get("ElementName") or "").lower()
    units = str(entry.get("BaseUnits") or "").lower()
    return name.startswith("fan") or units in {"rpm", "revolutions per minute"}


def fans_from_telemetry(raw: RawTelemetry) -> list[FanReading]:
    """Fan RPM from Dell Command | Monitor's DCIM_NumericSensor class.

    Read-only by design: on an XPS 13 the fan is owned by the EC/BIOS, and the
    SMI-based manual fan control that works on Alienware/G-series requires a
    signed kernel driver we deliberately do not ship.
    """
    fans: list[FanReading] = []
    for entry in raw.dcim_sensors:
        if not _dcim_is_fan(entry):
            continue
        rpm = _int(entry.get("CurrentReading"))
        if rpm is None:
            continue
        fans.append(
            FanReading(
                name=str(entry.get("ElementName") or entry.get("DeviceID") or "Fan"),
                rpm=rpm,
                source="wmi:dcim",
            )
        )
    return fans

def temperatures_from_telemetry(raw: RawTelemetry) -> list[TemperatureReading]:
    """Temperatures, preferring the most specific/trustworthy source available.

    Order of preference:
      1. LibreHardwareMonitor (real CPU package/core sensors), if running.
      2. Dell Command | Monitor temperature probes.
      3. ACPI thermal zone (coarse, but always present).
    """
    readings: list[TemperatureReading] = []

    for entry in raw.lhm_sensors:
        if str(entry.get("SensorType") or "").lower() != "temperature":
            continue
        value = _float(entry.get("Value"))
        if value is None:
            continue
        name = str(entry.get("Name") or "Sensor")
        if "average" in name.lower() or "max" in name.lower():
            # Keep max as a fallback candidate but do not crowd out package temp.
            continue
        readings.append(TemperatureReading(name=name, celsius=value, source="wmi:lhm"))
    if readings:
        return readings

    for entry in raw.dcim_sensors:
        if _dcim_is_fan(entry):
            continue
        name = str(entry.get("ElementName") or "")
        units = str(entry.get("BaseUnits") or "").lower()
        sensor_type = str(entry.get("SensorType") or "").lower()
        looks_like_temp = (
            "temp" in name.lower()
            or "temp" in sensor_type
            or units in {"degrees c", "degree c", "c", "celsius"}
        )
        if not looks_like_temp:
            continue
        value = _float(entry.get("CurrentReading"))
        if value is None:
            continue
        readings.append(
            TemperatureReading(name=name or "Temperature probe", celsius=value, source="wmi:dcim")
        )
    if readings:
        return readings

    for entry in raw.acpi_thermal:
        raw_value = _float(entry.get("CurrentTemperature"))
        if raw_value is None:
            continue
        # ACPI reports tenths of a degree Kelvin.
        celsius = raw_value / 10.0 - 273.15
        if -20 <= celsius <= 150:  # discard nonsense readings
            readings.append(
                TemperatureReading(name="ACPI thermal zone", celsius=celsius, source="wmi:acpi")
            )
    return readings


class WmiTelemetryBackend:
    """Reads battery, temperatures and fan speeds on Windows."""

    name = "wmi"

    def __init__(self, *, timeout: int = 25) -> None:
        self.timeout = timeout
        #: Set when the last refresh failed, so `doctor` can explain why.
        self.last_error: str | None = None
        self._cache: RawTelemetry | None = None

    @property
    def available(self) -> bool:
        return sys.platform == "win32"

    def refresh(self) -> RawTelemetry:
        if not self.available:
            self.last_error = "WMI telemetry is only available on Windows."
            return RawTelemetry()
        try:
            proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
                powershell_argv(TELEMETRY_SCRIPT),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                errors="replace",
                **hidden_process_kwargs(),
            )
        except FileNotFoundError:
            self.last_error = "PowerShell was not found on PATH."
            return RawTelemetry()
        except subprocess.TimeoutExpired:
            self.last_error = f"WMI query timed out after {self.timeout}s."
            return RawTelemetry()

        if proc.returncode != 0 and not (proc.stdout or "").strip():
            self.last_error = f"PowerShell exited {proc.returncode}: {(proc.stderr or '').strip()[:400]}"
            return RawTelemetry()

        self.last_error = None
        return parse_telemetry_json(proc.stdout)

    def _snapshot(self) -> RawTelemetry:
        if self._cache is None:
            self._cache = self.refresh()
        return self._cache

    def invalidate(self) -> None:
        """Drop the cache so the next read hits the hardware."""
        self._cache = None

    def read_battery(self) -> BatteryStatus | None:
        return battery_from_telemetry(self._snapshot())

    def read_temperatures(self) -> list[TemperatureReading]:
        return temperatures_from_telemetry(self._snapshot())

    def read_fans(self) -> list[FanReading]:
        return fans_from_telemetry(self._snapshot())

    def describe(self) -> list[str]:
        notes: list[str] = []
        if not self.available:
            notes.append("WMI telemetry unavailable (not running on Windows).")
            return notes
        raw = self._snapshot()
        if self.last_error:
            notes.append(f"WMI read problem: {self.last_error}")
        if not raw.battery and not raw.battery_static:
            notes.append("No battery information was returned by WMI.")
        if not raw.dcim_sensors and not raw.lhm_sensors and not raw.acpi_thermal:
            notes.append(
                "No temperature sensors found. Install Dell Command | Monitor or run "
                "LibreHardwareMonitor for CPU temperatures."
            )
        if not any(_dcim_is_fan(e) for e in raw.dcim_sensors):
            notes.append(
                "No fan sensors found. Fan speed reading needs Dell Command | Monitor; "
                "manual fan control is not supported on the XPS 13."
            )
        return notes
