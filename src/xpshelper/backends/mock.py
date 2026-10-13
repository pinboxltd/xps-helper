"""In-memory backend implementing both control and telemetry.

This is not just a test double. It powers two real product features:

* ``--backend mock`` lets the GUI and CLI be developed and demoed on any machine
  (including a non-Dell Linux box, like the one this was built on).
* The test suite asserts all *policy* -- threshold validation, persistence,
  degradation when a capability is missing -- against this backend, so those
  rules are verified without an XPS on the bench.

Everything is deterministic, so tests never sleep or retry.
"""

from __future__ import annotations

from ..model import (
    BatteryStatus,
    Capabilities,
    ChargeConfig,
    ChargeMode,
    FanReading,
    TemperatureReading,
    ThermalProfile,
)

ALL_PROFILES = tuple(ThermalProfile)
ALL_MODES = tuple(ChargeMode)


class MockBackend:
    """Simulates a Dell XPS 13 7390 2-in-1 running Dell Command | Configure."""

    name = "mock"

    def __init__(
        self,
        *,
        model: str = "XPS 13 7390 2-in-1",
        is_dell: bool = True,
        profiles: tuple[ThermalProfile, ...] = ALL_PROFILES,
        modes: tuple[ChargeMode, ...] = ALL_MODES,
        supports_advanced_schedule: bool = True,
        battery: bool = True,
        temperatures: bool = True,
        fans: bool = True,
        initial_profile: ThermalProfile | None = ThermalProfile.OPTIMIZED,
        initial_charge: ChargeConfig | None = None,
        battery_percent: int = 76,
        plugged_in: bool = True,
        fail_writes: bool = False,
        write_error: Exception | None = None,
    ) -> None:
        self.model = model
        self.is_dell = is_dell
        self._profiles = tuple(profiles)
        self._modes = tuple(modes)
        self._supports_adv = supports_advanced_schedule
        self._battery_enabled = battery
        self._temperatures_enabled = temperatures
        self._fans_enabled = fans
        self._profile = initial_profile
        self._charge = initial_charge or ChargeConfig(ChargeMode.ADAPTIVE)
        self._battery_percent = battery_percent
        self._plugged_in = plugged_in
        self._fail_writes = fail_writes
        self._write_error = write_error

        #: Every write that reached the "firmware", for assertions in tests.
        self.write_log: list[tuple[str, str]] = []

    # -- control ----------------------------------------------------------
    def probe(self) -> Capabilities:
        notes: list[str] = []
        if not self._profiles:
            notes.append("Firmware reports no thermal profiles.")
        if not self._modes:
            notes.append("Firmware reports no charge modes.")
        if self._supports_adv:
            notes.append("Advanced charge schedule is available.")
        return Capabilities(
            control_backend=self.name,
            telemetry_backend=self.name,
            thermal_profiles=self._profiles,
            charge_modes=self._modes,
            supports_advanced_schedule=self._supports_adv,
            battery_readable=self._battery_enabled,
            temperatures_readable=self._temperatures_enabled,
            fans_readable=self._fans_enabled,
            is_dell=self.is_dell,
            notes=notes,
        )

    def read_profile(self) -> ThermalProfile | None:
        return self._profile

    def read_charge_config(self) -> ChargeConfig | None:
        return self._charge

    def set_profile(self, profile: ThermalProfile) -> None:
        self._guard()
        if profile not in self._profiles:
            raise ValueError(f"{profile.value} is not supported on this (mock) machine")
        self._profile = profile
        self.write_log.append(("ThermalManagement", profile.token))

    def set_charge_config(self, config: ChargeConfig) -> None:
        self._guard()
        if config.mode not in self._modes:
            raise ValueError(f"{config.mode.value} is not supported on this (mock) machine")
        self._charge = config
        self.write_log.append(("PrimaryBattChargeCfg", config.token))

    def _guard(self) -> None:
        if self._fail_writes:
            raise self._write_error or RuntimeError("mock write failure")

    def seed(self, *, profile=None, charge=None) -> None:
        """Set simulated hardware state *without* recording a firmware write.

        Used to make ``--backend mock`` coherent across separate CLI runs: the
        mock's state lives in memory, so a new process would otherwise report
        the default profile right after the user set a different one, which
        looks like a bug rather than a simulation.
        """
        if profile is not None and profile in self._profiles:
            self._profile = profile
        if charge is not None and charge.mode in self._modes:
            self._charge = charge

    def supports_profile(self, profile) -> bool:
        return profile in self._profiles

    def supports_charge(self, config) -> bool:
        return config.mode in self._modes

    # -- telemetry --------------------------------------------------------
    def read_battery(self) -> BatteryStatus | None:
        if not self._battery_enabled:
            return None
        design = 52_000
        full = 47_500
        remaining = int(full * self._battery_percent / 100)
        return BatteryStatus(
            percent=self._battery_percent,
            plugged_in=self._plugged_in,
            charging=self._plugged_in and self._battery_percent < 100,
            health_percent=round(full / design * 100, 1),
            design_capacity_mwh=design,
            full_capacity_mwh=full,
            remaining_mwh=remaining,
            discharge_rate_mw=0 if self._plugged_in else 7_400,
            minutes_remaining=None if self._plugged_in else 214,
            source=self.name,
        )

    def read_temperatures(self) -> list[TemperatureReading]:
        if not self._temperatures_enabled:
            return []
        return [
            TemperatureReading("CPU Package", 47.5, source=self.name),
            TemperatureReading("Core Max", 49.0, source=self.name),
        ]

    def read_fans(self) -> list[FanReading]:
        if not self._fans_enabled:
            return []
        return [FanReading("Fan 1", 2413, source=self.name)]

    def describe(self) -> list[str]:
        return [f"Mock backend active (model: {self.model}). No hardware is being touched."]
