"""Domain model: the vocabulary the rest of the app speaks.

Design note
-----------
The enums store Dell's *firmware tokens* as their values. That means
``ThermalProfile.ULTRA.value == "UltraPerformance"`` -- you can hand an enum
member straight to cctk. The user-facing strings live in :attr:`label` and
:attr:`description`, so the UI never has to know a firmware string and the
protocol layer never has to know how to spell "Ultra Performance" for a human.

``from_token`` is deliberately lenient about *input* (accepting "Ultra
Performance", "ultraperformance", stray whitespace) and strict about *output*,
because the token we send to the BIOS must be exactly right.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Sequence

from . import constants as C
from .errors import ValidationError

#: Extra spellings accepted for thermal profiles, keyed by member name.
#: "performance"/"balanced" match the Linux ACPI platform_profile vocabulary so
#: the same CLI arguments work on both platforms.
_THERMAL_ALIASES: dict[str, tuple[str, ...]] = {
    "OPTIMIZED": ("balanced", "auto", "automatic", "default", "normal"),
    "QUIET": ("silent", "low", "powersave", "battery-saver", "battery"),
    "COOL": ("cool-bottom", "aggressive", "cold"),
    "ULTRA_PERFORMANCE": ("performance", "ultra", "turbo", "max", "beast"),
}

#: Extra spellings accepted for charge modes, keyed by member name.
_CHARGE_ALIASES: dict[str, tuple[str, ...]] = {
    "STANDARD": ("normal", "default"),
    "EXPRESS": ("expresscharge", "fast", "fastcharge", "quick"),
    "PRIMARILY_AC": ("primarilyac", "primarilyacuse", "primacuse", "ac", "desktop"),
    "ADAPTIVE": ("auto", "automatic"),
    "CUSTOM": ("manual", "thresholds", "limit"),
}


class _TokenEnum(str, Enum):
    """String enum whose value is the exact firmware token.

    ``from_token`` is lenient about what a *human or a different BIOS version*
    might call a mode (see the module-level alias tables), but the ``.value``
    that gets sent to the firmware is always exactly Dell's spelling.

    Note for maintainers: alias data lives in module-level dicts keyed by member
    name, deliberately *not* as a class attribute. A plain ``_ALIASES = {...}``
    inside an Enum body is silently turned into an enum *member* (verified
    against CPython), which would corrupt iteration and the capability lists.
    A classmethod is used instead, because functions are descriptors and are
    never treated as members.
    """

    @property
    def token(self) -> str:
        return str(self.value)

    @classmethod
    def _normalise(cls, text: str) -> str:
        return "".join(ch for ch in str(text).lower() if ch.isalnum())

    @classmethod
    def _aliases_for(cls, member: "_TokenEnum") -> tuple[str, ...]:
        """Alternative spellings accepted for ``member``. Overridden per enum."""
        return ()

    @classmethod
    def from_token(cls, text: str):
        """Match a token loosely; returns None if it is not one of ours.

        Dell sometimes reports or documents values with a parameter suffix --
        for example ``Custom:50-95`` in a supported-values list, or
        ``Enabled,Mon-09:00/17:00``. We retry with the part before the colon so
        those still resolve instead of being dropped from the capability list.
        """
        if text is None:
            return None
        candidates = [str(text)]
        head, sep, _ = str(text).partition(":")
        if sep and head.strip():
            candidates.append(head)
        for candidate in candidates:
            wanted = cls._normalise(candidate)
            if not wanted:
                continue
            for member in cls:
                if wanted in (cls._normalise(member.value), cls._normalise(member.name)):
                    return member
            for member in cls:
                for alias in cls._aliases_for(member):
                    if cls._normalise(alias) == wanted:
                        return member
        return None


class ThermalProfile(_TokenEnum):
    """Dell's four thermal/performance modes, in Dell's ordering."""

    OPTIMIZED = "Optimized"
    QUIET = "Quiet"
    COOL = "Cool"
    ULTRA_PERFORMANCE = "UltraPerformance"

    @classmethod
    def _aliases_for(cls, member: "_TokenEnum") -> tuple[str, ...]:
        return _THERMAL_ALIASES.get(member.name, ())

    @property
    def label(self) -> str:
        return {
            "Optimized": "Optimized",
            "Quiet": "Quiet",
            "Cool": "Cool",
            "UltraPerformance": "Ultra Performance",
        }[self.value]

    @property
    def description(self) -> str:
        return {
            "Optimized": "Balances performance, fan noise and temperature. Dell's default.",
            "Quiet": "Fans run slower and CPU power is capped (~11 W on XPS 13), so the "
            "machine is near-silent but slower and can run warmer.",
            "Cool": "Fans run faster to keep the chassis cooler, also with a CPU power cap.",
            "UltraPerformance": "Removes the CPU power cap for sustained performance. "
            "Fans will be louder and the machine warmer.",
        }[self.value]

    @property
    def caps_cpu_power(self) -> bool:
        """True for the profiles that limit sustained CPU power on this chassis.

        Documented behaviour for XPS 13 (9370/7390): Quiet and Cool hold PL1
        around 11 W with ~18 W boost, so "Quiet" is a real performance decision,
        not merely a fan curve.
        """
        return self in (ThermalProfile.QUIET, ThermalProfile.COOL)


class ChargeMode(_TokenEnum):
    """Dell primary-battery charge configuration modes."""

    STANDARD = "Standard"
    EXPRESS = "Express"
    PRIMARILY_AC = "PrimAcUse"
    ADAPTIVE = "Adaptive"
    CUSTOM = "Custom"

    @classmethod
    def _aliases_for(cls, member: "_TokenEnum") -> tuple[str, ...]:
        return _CHARGE_ALIASES.get(member.name, ())

    @property
    def label(self) -> str:
        return {
            "Standard": "Standard",
            "Express": "Express Charge",
            "PrimAcUse": "Primarily AC Use",
            "Adaptive": "Adaptive",
            "Custom": "Custom",
        }[self.value]

    @property
    def description(self) -> str:
        return {
            "Standard": "Charges to 100% at a normal rate.",
            "Express": "Dell's fast-charge mode: reaches 100% as quickly as possible. "
            "Generates the most heat and the most battery wear.",
            "PrimAcUse": "Caps charging at roughly 80% for machines that mostly run "
            "on AC power. The simplest way to extend battery lifespan.",
            "Adaptive": "Dell's automatic mode: learns your charging routine and "
            "optimises when the battery reaches 100%.",
            "Custom": "You choose the start and stop thresholds.",
        }[self.value]

    @property
    def needs_thresholds(self) -> bool:
        return self is ChargeMode.CUSTOM


@dataclass(frozen=True)
class ChargeConfig:
    """A battery charge configuration, validating Dell's own threshold rules.

    Dell enforces: start 50-95, stop 55-100, and stop - start >= 5. We check
    these client-side so a bad value produces a clear message instead of a
    cryptic cctk exit code.
    """

    mode: ChargeMode
    start: int | None = None
    stop: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.mode, ChargeMode):
            raise ValidationError(f"mode must be a ChargeMode, got {self.mode!r}")

        if self.mode.needs_thresholds:
            if self.start is None or self.stop is None:
                raise ValidationError("Custom charge mode requires both a start and a stop threshold.")
            if not (C.CUSTOM_START_MIN <= self.start <= C.CUSTOM_START_MAX):
                raise ValidationError(
                    f"Custom start threshold must be {C.CUSTOM_START_MIN}-{C.CUSTOM_START_MAX}%, "
                    f"got {self.start}%."
                )
            if not (C.CUSTOM_STOP_MIN <= self.stop <= C.CUSTOM_STOP_MAX):
                raise ValidationError(
                    f"Custom stop threshold must be {C.CUSTOM_STOP_MIN}-{C.CUSTOM_STOP_MAX}%, "
                    f"got {self.stop}%."
                )
            if self.stop - self.start < C.CUSTOM_MIN_GAP:
                raise ValidationError(
                    f"Custom thresholds must be at least {C.CUSTOM_MIN_GAP}% apart "
                    f"(start {self.start}%, stop {self.stop}%)."
                )
        elif self.start is not None or self.stop is not None:
            raise ValidationError(
                f"Thresholds only apply to Custom mode; {self.mode.label} takes no thresholds."
            )

    @property
    def token(self) -> str:
        """The exact string to pass as ``--PrimaryBattChargeCfg=``."""
        if self.mode.needs_thresholds:
            return f"{self.mode.value}:{self.start}-{self.stop}"
        return self.mode.value

    @property
    def describe(self) -> str:
        if self.mode.needs_thresholds:
            return f"Custom: charge from {self.start}% up to {self.stop}%"
        return self.mode.label

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode.value, "start": self.start, "stop": self.stop, "token": self.token}

    @classmethod
    def from_token(cls, text: str) -> "ChargeConfig | None":
        """Parse cctk's reported value, e.g. ``Custom:75-80`` or ``PrimAcUse``."""
        if not text:
            return None
        raw = str(text).strip()
        if ":" in raw:
            head, _, tail = raw.partition(":")
            mode = ChargeMode.from_token(head)
            if mode is None:
                return None
            numbers: Sequence[str] = [p for p in tail.replace("%", "").split("-") if p.strip()]
            try:
                if len(numbers) == 2:
                    return cls(mode=mode, start=int(numbers[0]), stop=int(numbers[1]))
            except (ValueError, ValidationError):
                return None
            return None
        mode = ChargeMode.from_token(raw)
        if mode is None or mode.needs_thresholds:
            return None
        return cls(mode=mode)


@dataclass(frozen=True)
class BatteryStatus:
    percent: int | None = None
    plugged_in: bool | None = None
    charging: bool | None = None
    health_percent: float | None = None
    design_capacity_mwh: int | None = None
    full_capacity_mwh: int | None = None
    remaining_mwh: int | None = None
    discharge_rate_mw: int | None = None
    minutes_remaining: int | None = None
    source: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "percent": self.percent,
            "plugged_in": self.plugged_in,
            "charging": self.charging,
            "health_percent": self.health_percent,
            "design_capacity_mwh": self.design_capacity_mwh,
            "full_capacity_mwh": self.full_capacity_mwh,
            "remaining_mwh": self.remaining_mwh,
            "discharge_rate_mw": self.discharge_rate_mw,
            "minutes_remaining": self.minutes_remaining,
            "source": self.source,
        }


@dataclass(frozen=True)
class TemperatureReading:
    name: str
    celsius: float
    source: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "celsius": round(self.celsius, 1), "source": self.source}


@dataclass(frozen=True)
class FanReading:
    name: str
    rpm: int
    source: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "rpm": self.rpm, "source": self.source}


@dataclass
class Capabilities:
    """What this exact machine + install can actually do.

    Built by probing, never assumed: the same XPS 13 model can differ by BIOS
    version, and cctk reports per-model supported values.
    """

    control_backend: str = "none"
    telemetry_backend: str = "none"
    thermal_profiles: tuple[ThermalProfile, ...] = ()
    charge_modes: tuple[ChargeMode, ...] = ()
    supports_advanced_schedule: bool = False
    battery_readable: bool = False
    temperatures_readable: bool = False
    fans_readable: bool = False
    is_dell: bool | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def supports_custom_thresholds(self) -> bool:
        return ChargeMode.CUSTOM in self.charge_modes

    @property
    def can_control(self) -> bool:
        return bool(self.thermal_profiles or self.charge_modes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "control_backend": self.control_backend,
            "telemetry_backend": self.telemetry_backend,
            "thermal_profiles": [p.value for p in self.thermal_profiles],
            "charge_modes": [m.value for m in self.charge_modes],
            "supports_custom_thresholds": self.supports_custom_thresholds,
            "supports_advanced_schedule": self.supports_advanced_schedule,
            "battery_readable": self.battery_readable,
            "temperatures_readable": self.temperatures_readable,
            "fans_readable": self.fans_readable,
            "is_dell": self.is_dell,
            "notes": list(self.notes),
        }


@dataclass
class SystemStatus:
    model: str | None = None
    is_dell: bool | None = None
    profile: ThermalProfile | None = None
    charge: ChargeConfig | None = None
    battery: BatteryStatus | None = None
    temperatures: list[TemperatureReading] = field(default_factory=list)
    fans: list[FanReading] = field(default_factory=list)
    capabilities: Capabilities | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def cpu_temperature(self) -> TemperatureReading | None:
        """Best available CPU temperature, preferring explicit CPU sensors."""
        if not self.temperatures:
            return None
        ranked = sorted(self.temperatures, key=_cpu_temperature_rank)
        return ranked[0]

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "is_dell": self.is_dell,
            "profile": self.profile.value if self.profile else None,
            "profile_label": self.profile.label if self.profile else None,
            "charge": self.charge.to_dict() if self.charge else None,
            "battery": self.battery.to_dict() if self.battery else None,
            "temperatures": [t.to_dict() for t in self.temperatures],
            "fans": [f.to_dict() for f in self.fans],
            "capabilities": self.capabilities.to_dict() if self.capabilities else None,
            "warnings": list(self.warnings),
        }


_CPU_TEMP_PRIORITY = ("cpu package", "cpu", "core max", "package", "core average", "acpi")


def _cpu_temperature_rank(reading: TemperatureReading) -> tuple[int, float]:
    name = reading.name.lower()
    for index, needle in enumerate(_CPU_TEMP_PRIORITY):
        if needle in name:
            return (index, 0.0)
    return (len(_CPU_TEMP_PRIORITY), 0.0)


def parse_profile_list(tokens: Iterable[str]) -> tuple[ThermalProfile, ...]:
    """Map raw token strings to enum members, dropping anything unrecognised."""
    found: list[ThermalProfile] = []
    for token in tokens:
        member = ThermalProfile.from_token(token)
        if member is not None and member not in found:
            found.append(member)
    return tuple(found)


def parse_charge_mode_list(tokens: Iterable[str]) -> tuple[ChargeMode, ...]:
    found: list[ChargeMode] = []
    for token in tokens:
        member = ChargeMode.from_token(token)
        if member is not None and member not in found:
            found.append(member)
    return tuple(found)
