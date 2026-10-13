"""Linux telemetry backend (sysfs + psutil).

Not the primary target -- the app is aimed at Windows 11 -- but it costs little
and buys two things: the same ``status``/``doctor`` code paths can be exercised
on a Linux dev box, and an XPS dual-booting Linux still gets monitoring.

Control on Linux is *not* done here: that is cctk plus the kernel's
``platform_profile`` interface, which the cctk backend and ``doctor`` cover.
"""

from __future__ import annotations

import glob
import logging
import os
from pathlib import Path

from ..model import BatteryStatus, FanReading, TemperatureReading

log = logging.getLogger(__name__)

POWER_SUPPLY_GLOB = "/sys/class/power_supply/BAT*"


def _read_text(path: str | Path) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def _read_int(path: str | Path) -> int | None:
    raw = _read_text(path)
    if raw is None:
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


class LinuxTelemetryBackend:
    """Reads battery, temperatures and fans from sysfs and psutil on Linux."""

    name = "linux"

    @property
    def available(self) -> bool:
        return os.name == "posix" and Path("/sys/class/power_supply").is_dir()

    def _battery_dir(self) -> Path | None:
        candidates = sorted(glob.glob(POWER_SUPPLY_GLOB))
        return Path(candidates[0]) if candidates else None

    def read_battery(self) -> BatteryStatus | None:
        base = self._battery_dir()
        if base is None:
            # Fall back to psutil, which also works on machines without BAT*.
            try:
                import psutil

                info = psutil.sensors_battery()
            except Exception:  # noqa: BLE001
                return None
            if info is None:
                return None
            return BatteryStatus(
                percent=int(info.percent),
                plugged_in=bool(info.power_plugged),
                charging=bool(info.power_plugged) and info.percent < 100,
                minutes_remaining=None
                if info.secsleft in (None, getattr(psutil, "POWER_TIME_UNKNOWN", -1))
                else int(info.secsleft // 60),
                source="psutil",
            )

        # Newer kernels expose charge_*; older ones energy_*. Support both.
        percent = _read_int(base / "capacity")
        status = (_read_text(base / "status") or "").lower()
        energy_full = _read_int(base / "energy_full")
        energy_now = _read_int(base / "energy_now")
        charge_full = _read_int(base / "charge_full")
        charge_now = _read_int(base / "charge_now")
        full = energy_full or charge_full
        now = energy_now or charge_now
        design = _read_int(base / "energy_full_design") or _read_int(base / "charge_full_design")
        power_now = _read_int(base / "power_now")

        if percent is None and full and now:
            percent = int(round(now / full * 100))

        health = None
        if design and full and design > 0:
            health = round(full / design * 100, 1)

        minutes = None
        if power_now and now and power_now > 0 and status == "discharging":
            minutes = int(round(now / power_now * 60))

        return BatteryStatus(
            percent=percent,
            plugged_in=status in {"charging", "full", "not charging"},
            charging=status == "charging",
            health_percent=health,
            design_capacity_mwh=design,
            full_capacity_mwh=full,
            remaining_mwh=now,
            discharge_rate_mw=None if status != "discharging" else power_now,
            minutes_remaining=minutes,
            source="sysfs",
        )

    def read_temperatures(self) -> list[TemperatureReading]:
        readings: list[TemperatureReading] = []
        try:
            import psutil

            sensors = psutil.sensors_temperatures() or {}
            for chip, entries in sensors.items():
                for entry in entries:
                    if entry.current is None:
                        continue
                    label = entry.label or chip
                    readings.append(
                        TemperatureReading(
                            name=f"{chip}: {label}" if entry.label else chip,
                            celsius=float(entry.current),
                            source="psutil",
                        )
                    )
        except Exception:  # noqa: BLE001 - sensor access is best-effort
            pass

        if readings:
            return readings

        for zone in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
            celsius = _read_int(Path(zone) / "temp")
            if celsius is None:
                continue
            name = _read_text(Path(zone) / "type") or Path(zone).name
            readings.append(
                TemperatureReading(name=name, celsius=celsius / 1000.0, source="sysfs")
            )
        return readings

    def read_fans(self) -> list[FanReading]:
        fans: list[FanReading] = []
        try:
            import psutil

            sensors = psutil.sensors_fans() or {}
            for chip, entries in sensors.items():
                for entry in entries:
                    if entry.current is None:
                        continue
                    label = entry.label or chip
                    fans.append(FanReading(name=label, rpm=int(entry.current), source="psutil"))
        except Exception:  # noqa: BLE001
            pass
        return fans

    def describe(self) -> list[str]:
        notes: list[str] = []
        if not self.available:
            notes.append("Linux telemetry unavailable (no /sys/class/power_supply).")
            return notes
        if self._battery_dir() is None:
            notes.append("No battery device found under /sys/class/power_supply/BAT*.")
        return notes
