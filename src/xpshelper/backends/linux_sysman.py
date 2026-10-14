"""Linux native control via the kernel's firmware-attributes class.

The Linux kernel's ``dell-wmi-sysman`` driver (and the generic
``firmware_attributes_class``) exposes the *same* BIOS attributes the Windows
backend drives -- ``ThermalManagement``, ``PrimaryBattChargeCfg`` and friends --
as writable sysfs files:

    /sys/class/firmware-attributes/dell-wmi-sysman/attributes/ThermalManagement/
        current_value     (read/write)
        possible_values   (read-only: the supported value list)
        display_name
        type

So on Linux this needs no Dell software either: no cctk, no libsmbios. Writing
requires root, and when a BIOS administrator password is set the password is
written to the class-level ``authentication`` file before the value.

This module is fully testable off-hardware by pointing it at a temporary
directory laid out like the real sysfs tree.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from .. import constants as C
from ..errors import AttributeNotSupported, BiosPasswordRequired, CctkError
from ..model import (
    Capabilities,
    ChargeConfig,
    ChargeMode,
    ThermalProfile,
    parse_charge_mode_list,
    parse_profile_list,
)

log = logging.getLogger(__name__)

THERMAL_ATTRIBUTE_CANDIDATES = ("ThermalManagement",)
CHARGE_ATTRIBUTE_CANDIDATES = ("PrimaryBattChargeCfg", "PrimaryBatteryChargeCfg")
ADV_CHARGE_ATTRIBUTE = "AdvBatteryChargeCfg"


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def discover_attributes(root: Path) -> dict[str, Path]:
    """Map attribute name -> its directory under any provider.

    ``root`` is normally ``/sys/class/firmware-attributes``; each provider (for
    example ``dell-wmi-sysman``) has an ``attributes/<name>/`` subdirectory.
    """
    found: dict[str, Path] = {}
    try:
        providers = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return found
    for provider in providers:
        attributes_dir = provider / "attributes"
        try:
            entries = sorted(p for p in attributes_dir.iterdir() if p.is_dir())
        except OSError:
            continue
        for entry in entries:
            # Only trust directories that actually expose a current_value.
            if (entry / "current_value").exists():
                found.setdefault(entry.name, entry)
    return found


def parse_possible_values(raw: str | None) -> list[str]:
    """Split the sysfs possible_values file.

    Values are separated by whitespace (and sometimes a comma); quoted entries
    can contain spaces, e.g. ``"Primarily AC Use"``.
    """
    if not raw:
        return []
    text = raw.replace(",", " ")
    values: list[str] = []
    buffer = ""
    in_quotes = False
    for char in text:
        if char == '"':
            in_quotes = not in_quotes
            continue
        if char.isspace() and not in_quotes:
            if buffer:
                values.append(buffer)
                buffer = ""
            continue
        buffer += char
    if buffer:
        values.append(buffer)
    return values


class LinuxSysmanBackend:
    """Control backend using the kernel firmware-attributes sysfs interface."""

    name = "linux-sysman"

    def __init__(
        self,
        root: Path | str = C.LINUX_FIRMWARE_ATTRIBUTES,
        *,
        bios_password: str | None = None,
    ) -> None:
        self.root = Path(root)
        self.bios_password = bios_password or os.environ.get(C.ENV_BIOS_PASSWORD) or None
        self._attributes: dict[str, Path] | None = None

    # -- discovery --------------------------------------------------------
    @property
    def attributes(self) -> dict[str, Path]:
        if self._attributes is None:
            self._attributes = discover_attributes(self.root)
        return self._attributes

    @property
    def available(self) -> bool:
        return bool(self.attributes)

    def find_attribute_dir(self, candidates) -> Path | None:
        for name in candidates:
            path = self.attributes.get(name)
            if path is not None:
                return path
        return None

    def _provider_dir(self, attribute_dir: Path) -> Path:
        """The provider (e.g. dell-wmi-sysman) directory, which holds auth."""
        try:
            return attribute_dir.parent.parent
        except IndexError:  # pragma: no cover - defensive
            return attribute_dir

    def is_elevated(self) -> bool:
        try:
            return os.geteuid() == 0
        except AttributeError:  # pragma: no cover - non-posix
            return False

    # -- reads ------------------------------------------------------------
    def probe(self, **_kwargs) -> Capabilities:
        caps = Capabilities(control_backend=self.name)
        if not self.available:
            caps.notes.append(
                f"No BIOS attributes found under {self.root}. This machine may not "
                "support WMI-ACPI BIOS attributes, or the kernel module "
                "dell-wmi-sysman is not loaded."
            )
            return caps

        names = sorted(self.attributes)
        caps.notes.append(f"BIOS attributes exposed: {', '.join(names)}")

        thermal = self.find_attribute_dir(THERMAL_ATTRIBUTE_CANDIDATES)
        if thermal is not None:
            caps.supports_thermal = True
            values = parse_possible_values(_read_text(thermal / "possible_values"))
            caps.thermal_profiles = (
                parse_profile_list(values) if values else tuple(ThermalProfile)
            )
            if not values:
                caps.notes.append("ThermalManagement reports no possible_values.")
        else:
            caps.supports_thermal = False
            caps.notes.append(
                "The kernel does not expose a ThermalManagement attribute, so thermal "
                "profiles cannot be changed on this machine."
            )

        charge = self.find_attribute_dir(CHARGE_ATTRIBUTE_CANDIDATES)
        if charge is not None:
            caps.supports_charge = True
            values = parse_possible_values(_read_text(charge / "possible_values"))
            caps.charge_modes = parse_charge_mode_list(values) if values else tuple(ChargeMode)
            if not values:
                caps.notes.append("The charge attribute reports no possible_values.")
        else:
            caps.supports_charge = False
            caps.notes.append(
                "The kernel does not expose a battery charge configuration attribute."
            )

        caps.supports_advanced_schedule = ADV_CHARGE_ATTRIBUTE in self.attributes
        return caps

    def read_profile(self) -> ThermalProfile | None:
        path = self.find_attribute_dir(THERMAL_ATTRIBUTE_CANDIDATES)
        if path is None:
            return None
        return ThermalProfile.from_token(_read_text(path / "current_value") or "")

    def read_charge_config(self) -> ChargeConfig | None:
        path = self.find_attribute_dir(CHARGE_ATTRIBUTE_CANDIDATES)
        if path is None:
            return None
        return ChargeConfig.from_token(_read_text(path / "current_value") or "")

    def read_raw_value(self, attribute: str) -> str | None:
        path = self.attributes.get(attribute)
        if path is None:
            return None
        return _read_text(path / "current_value")

    # -- helpers used by `doctor` ----------------------------------------
    @staticmethod
    def possible_values_for(attribute_dir: Path) -> list[str]:
        return parse_possible_values(_read_text(attribute_dir / "possible_values"))

    @staticmethod
    def current_value_for(attribute_dir: Path) -> str | None:
        return _read_text(attribute_dir / "current_value")

    # -- writes -----------------------------------------------------------
    def _write_value(self, attribute_dir: Path, value: str) -> None:
        target = attribute_dir / "current_value"
        try:
            target.write_text(value, encoding="utf-8")
            return
        except PermissionError as exc:
            # A BIOS admin password may be required; supply it and retry once.
            if self.bios_password and self._authenticate(attribute_dir):
                try:
                    target.write_text(value, encoding="utf-8")
                    return
                except OSError as retry_exc:
                    raise self._write_error(attribute_dir.name, value, retry_exc) from retry_exc
            raise self._write_error(attribute_dir.name, value, exc) from exc
        except OSError as exc:
            raise self._write_error(attribute_dir.name, value, exc) from exc

    def _authenticate(self, attribute_dir: Path) -> bool:
        """Write the BIOS password to the provider's authentication file."""
        auth_file = self._provider_dir(attribute_dir) / "authentication"
        try:
            auth_file.write_text(self.bios_password or "", encoding="utf-8")
            return True
        except OSError:
            return False

    @staticmethod
    def _write_error(attribute: str, value: str, exc: OSError) -> Exception:
        if isinstance(exc, PermissionError):
            return BiosPasswordRequired(
                f"Cannot write {attribute}={value}: permission denied. Run as root, "
                "or provide the BIOS administrator password if one is set.",
                output=str(exc),
            )
        if isinstance(exc, OSError) and getattr(exc, "errno", None) == 22:
            return AttributeNotSupported(
                f"The BIOS rejected {attribute}={value} as an invalid value.",
                output=str(exc),
            )
        return CctkError(f"Could not set {attribute}={value}: {exc}", output=str(exc))

    def set_attribute(self, attribute: str, value: str) -> None:
        attribute_dir = self.attributes.get(attribute)
        if attribute_dir is None:
            raise AttributeNotSupported(
                f"This machine does not expose a BIOS attribute called {attribute}."
            )
        self._write_value(attribute_dir, value)

    def set_profile(self, profile: ThermalProfile) -> None:
        attribute_dir = self.find_attribute_dir(THERMAL_ATTRIBUTE_CANDIDATES)
        if attribute_dir is None:
            raise AttributeNotSupported("This machine does not expose ThermalManagement.")
        self._write_value(attribute_dir, profile.token)

    def set_charge_config(self, config: ChargeConfig) -> None:
        attribute_dir = self.find_attribute_dir(CHARGE_ATTRIBUTE_CANDIDATES)
        if attribute_dir is None:
            raise AttributeNotSupported(
                "This machine does not expose a battery charge configuration attribute."
            )
        self._write_value(attribute_dir, config.token)
