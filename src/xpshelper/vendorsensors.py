"""Read real sensors from third-party monitor processes that already have a driver.

Why this module exists
----------------------
This machine's firmware exposes no wattages and no fan speeds, and Windows gives
user mode no way to execute RDMSR. Everything that shows package power, the
PL1/PL2 power limits or per-core temperatures -- HWMonitor, HWiNFO,
LibreHardwareMonitor, ThrottleStop -- installs a **kernel-mode driver** and reads
the CPU's model-specific registers from ring 0. On this machine HWMonitor's
``cpuz166`` driver is what supplies those numbers.

XPS Helper does not ship a driver, and should not: that is a privilege escalation
surface far larger than a thermal-mode switch needs. What it *can* do is read from
a provider the user already runs, through the interface that provider publishes:

* **LibreHardwareMonitor** -- ``root\\LibreHardwareMonitor :: Sensor`` (WMI)
* **OpenHardwareMonitor** -- ``root\\OpenHardwareMonitor :: Sensor`` (WMI)
* **HWiNFO** -- ``HKLM\\SOFTWARE\\HWiNFO64\\VSB`` (registry, "Report to Registry")

HWMonitor (CPUID) publishes no interface at all, so it can never be a source here
even though it is often the one people have.

Fail closed
-----------
The HWiNFO registry layout is written by a program we do not control and cannot
verify on the machine this was developed on, so the reader is written to return
**nothing** when the shape is unexpected rather than to guess. A number that might
be the wrong sensor is worse than a blank field in a tool that changes firmware
settings.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

#: Registry keys HWiNFO writes when "Report to Registry" is enabled.
HWINFO_KEYS = (
    r"SOFTWARE\HWiNFO64\VSB",
    r"SOFTWARE\HWiNFO32\VSB",
)

#: Sensor kinds, normalised from the various provider spellings.
KIND_TEMPERATURE = "temperature"
KIND_POWER = "power"
KIND_VOLTAGE = "voltage"
KIND_FAN = "fan"
KIND_CLOCK = "clock"
KIND_OTHER = "other"

#: HWiNFO's ValueType codes. Documented by its own "Report to Registry" feature.
_HWINFO_TYPES = {
    0: KIND_OTHER,
    1: KIND_TEMPERATURE,
    2: KIND_VOLTAGE,
    3: KIND_FAN,
    4: KIND_OTHER,      # current
    5: KIND_POWER,
    6: KIND_CLOCK,
    7: KIND_OTHER,      # usage
}


@dataclass(frozen=True)
class VendorSensor:
    """One reading from a third-party provider."""

    name: str
    value: float
    kind: str
    provider: str

    @property
    def unit(self) -> str:
        return {
            KIND_TEMPERATURE: "C",
            KIND_POWER: "W",
            KIND_VOLTAGE: "V",
            KIND_FAN: "RPM",
            KIND_CLOCK: "MHz",
        }.get(self.kind, "")


def sensors_from_wmi_entries(entries, provider: str) -> list[VendorSensor]:
    """Normalise LibreHardwareMonitor/OpenHardwareMonitor ``Sensor`` rows.

    Both projects publish the same class shape: ``Name``, ``SensorType``,
    ``Value``, plus a ``Parent`` naming the hardware. Only temperature, power,
    voltage and fan rows are kept -- clocks and loads are not what this reports.
    """
    out: list[VendorSensor] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        raw_type = str(entry.get("SensorType") or "").strip().lower()
        kind = {
            "temperature": KIND_TEMPERATURE,
            "power": KIND_POWER,
            "voltage": KIND_VOLTAGE,
            "fan": KIND_FAN,
        }.get(raw_type)
        if kind is None:
            continue
        try:
            value = float(entry.get("Value"))
        except (TypeError, ValueError):
            continue
        name = str(entry.get("Name") or "").strip() or raw_type
        parent = str(entry.get("Parent") or "").strip()
        label = f"{parent}: {name}" if parent else name
        out.append(VendorSensor(name=label, value=value, kind=kind, provider=provider))
    return out


def _hwinfo_rows(store) -> dict[int, dict[str, object]]:
    """Group HWiNFO's registry values into rows keyed by sensor index.

    The published layout is a flat key holding ``Sensor<N>`` (label),
    ``Value<N>`` and ``ValueType<N>``. Some builds have been seen using
    ``Label<N>``; both spellings are accepted, and an index missing a label or a
    value is dropped.
    """
    rows: dict[int, dict[str, object]] = {}
    pattern = re.compile(r"^(sensor|label|value|valuetype)(\d+)$", re.IGNORECASE)
    for name, value in store.items():
        match = pattern.match(str(name))
        if not match:
            continue
        field_name, index = match.group(1).lower(), int(match.group(2))
        row = rows.setdefault(index, {})
        if field_name in {"sensor", "label"}:
            row["label"] = value
        elif field_name == "value":
            row["value"] = value
        else:
            row["type"] = value
    return rows


def hwinfo_sensors() -> list[VendorSensor]:
    """Read HWiNFO's registry report. Returns nothing if it is not there."""
    if not _is_windows():
        return []
    try:
        import winreg
    except ImportError:  # pragma: no cover - non-Windows
        return []

    for path in HWINFO_KEYS:
        store: dict[str, object] = {}
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
                index = 0
                while True:
                    try:
                        name, value, value_type = winreg.EnumValue(key, index)
                    except OSError:
                        break
                    store[name] = _decode(value, value_type)
                    index += 1
        except OSError:
            continue
        if not store:
            continue

        sensors: list[VendorSensor] = []
        for _index, row in sorted(_hwinfo_rows(store).items()):
            label = str(row.get("label") or "").strip()
            if not label:
                continue
            try:
                value = float(row.get("value"))
            except (TypeError, ValueError):
                continue
            try:
                kind = _HWINFO_TYPES.get(int(row.get("type") or 0), KIND_OTHER)
            except (TypeError, ValueError):
                kind = KIND_OTHER
            if kind == KIND_OTHER:
                continue
            sensors.append(VendorSensor(name=label, value=value, kind=kind, provider="hwinfo"))
        if sensors:
            return sensors
        # The key exists but did not yield anything usable. Reporting nothing is
        # the safe outcome: we do not know this layout well enough to trust it.
        log.debug("HWiNFO registry key %s had %d values but no usable sensors", path, len(store))
    return []


def _decode(value, value_type) -> object:
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", "replace").strip("\x00")
        except Exception:  # noqa: BLE001
            return ""
    if value_type == 4:  # REG_DWORD
        return value
    return value


def _is_windows() -> bool:
    import sys

    return sys.platform == "win32"


# ---------------------------------------------------------------------------
# Interpretation
# ---------------------------------------------------------------------------
def package_temperature(sensors: list[VendorSensor]) -> VendorSensor | None:
    """The CPU package temperature, if a provider reports one."""
    for sensor in sensors:
        if sensor.kind != KIND_TEMPERATURE:
            continue
        name = sensor.name.lower()
        if "package" in name or name.endswith(": cpu") or "cpu (" in name:
            return sensor
    return None


def package_power(sensors: list[VendorSensor]) -> VendorSensor | None:
    """The CPU package power draw, if a provider reports one."""
    for sensor in sensors:
        if sensor.kind != KIND_POWER:
            continue
        if "package" in sensor.name.lower():
            return sensor
    for sensor in sensors:
        if sensor.kind == KIND_POWER:
            return sensor
    return None


def power_limit(sensors: list[VendorSensor], which: str) -> VendorSensor | None:
    """The PL1/PL2 power limit, if a provider reports it.

    These are firmware MSRs (``MSR_PKG_POWER_LIMIT``). Watching them move is the
    test for whether a thermal-mode change actually alters the power limit, which
    temperature alone cannot answer.
    """
    wanted = which.lower()
    for sensor in sensors:
        name = sensor.name.lower()
        if f"pl{wanted}" in name or f"power limit {wanted}" in name:
            return sensor
    return None
