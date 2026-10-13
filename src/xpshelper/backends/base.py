"""Backend protocol.

Two capabilities are separated on purpose:

* **Control** -- writing thermal profile and charge configuration. Implemented by
  the cctk backend (and the mock).
* **Telemetry** -- reading battery, temperatures and fan speeds. Implemented by
  the WMI backend, the psutil/sysfs backend, and the mock.

A machine may support one without the other (e.g. cctk not installed but WMI
reads fine), so the service composes them instead of demanding a single backend
that does everything.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..model import (
    BatteryStatus,
    Capabilities,
    ChargeConfig,
    FanReading,
    SystemStatus,
    TemperatureReading,
    ThermalProfile,
)


@runtime_checkable
class ControlBackend(Protocol):
    """Write-side interface."""

    name: str

    def probe(self) -> Capabilities:
        """Discover what this machine's firmware actually supports."""

    def read_profile(self) -> ThermalProfile | None:
        """Currently active thermal profile, or None if unreadable."""

    def read_charge_config(self) -> ChargeConfig | None:
        """Currently configured charge mode/thresholds, or None if unreadable."""

    def set_profile(self, profile: ThermalProfile) -> None:
        """Apply a thermal profile. Raises on failure."""

    def set_charge_config(self, config: ChargeConfig) -> None:
        """Apply a battery charge configuration. Raises on failure."""


@runtime_checkable
class TelemetryBackend(Protocol):
    """Read-side interface. All methods return empty/None rather than raising."""

    name: str

    def read_battery(self) -> BatteryStatus | None: ...

    def read_temperatures(self) -> list[TemperatureReading]: ...

    def read_fans(self) -> list[FanReading]: ...

    def describe(self) -> list[str]:
        """Human-readable notes about what this backend could and could not read."""


class NullControlBackend:
    """Control backend used when no real one is available.

    Exists so the app degrades to "read-only monitoring + clear explanation"
    instead of crashing on a machine without cctk.
    """

    name = "none"

    def __init__(self, reason: str = "No control backend available.") -> None:
        self.reason = reason

    def probe(self) -> Capabilities:
        return Capabilities(control_backend=self.name, notes=[self.reason])

    def read_profile(self) -> ThermalProfile | None:
        return None

    def read_charge_config(self) -> ChargeConfig | None:
        return None

    def set_profile(self, profile: ThermalProfile) -> None:
        raise NotImplementedError(self.reason)

    def set_charge_config(self, config: ChargeConfig) -> None:
        raise NotImplementedError(self.reason)


class NullTelemetryBackend:
    """Telemetry backend used when nothing can be read."""

    name = "none"

    def __init__(self, reason: str = "No telemetry backend available.") -> None:
        self.reason = reason

    def read_battery(self) -> BatteryStatus | None:
        return None

    def read_temperatures(self) -> list[TemperatureReading]:
        return []

    def read_fans(self) -> list[FanReading]:
        return []

    def describe(self) -> list[str]:
        return [self.reason]


def collect_status(
    control: ControlBackend,
    telemetry: TelemetryBackend,
    *,
    capabilities: Capabilities | None = None,
    model: str | None = None,
    is_dell: bool | None = None,
) -> SystemStatus:
    """Assemble a full status snapshot, tolerating every partial failure.

    Monitoring must never raise: a user watching their battery level cares more
    about seeing temperature than about a clean exception path.
    """
    warnings: list[str] = []

    try:
        profile = control.read_profile()
    except Exception as exc:  # noqa: BLE001 - monitoring must not raise
        profile = None
        warnings.append(f"Could not read thermal profile: {exc}")

    try:
        charge = control.read_charge_config()
    except Exception as exc:  # noqa: BLE001
        charge = None
        warnings.append(f"Could not read charge configuration: {exc}")

    try:
        battery = telemetry.read_battery()
    except Exception as exc:  # noqa: BLE001
        battery = None
        warnings.append(f"Could not read battery: {exc}")

    try:
        temperatures = telemetry.read_temperatures()
    except Exception as exc:  # noqa: BLE001
        temperatures = []
        warnings.append(f"Could not read temperatures: {exc}")

    try:
        fans = telemetry.read_fans()
    except Exception as exc:  # noqa: BLE001
        fans = []
        warnings.append(f"Could not read fan speeds: {exc}")

    return SystemStatus(
        model=model,
        is_dell=is_dell,
        profile=profile,
        charge=charge,
        battery=battery,
        temperatures=list(temperatures),
        fans=list(fans),
        capabilities=capabilities,
        warnings=warnings,
    )
