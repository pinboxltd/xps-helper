"""`doctor` diagnostics.

The purpose of this module is to make a hardware problem *self-reporting*. It
was written on a machine that is not an XPS, so every assumption about the real
laptop is a hypothesis that has to be checked on the target. When a check cannot
be completed, the report includes the raw output from Dell's own tool, so the
failure can be diagnosed (and the parser fixed) without guesswork.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any

from . import constants as C
from . import platform as platform_info
from .backends.base import NullControlBackend
from .backends.cctk import CctkBackend
from .backends.composite import CompositeControlBackend
from .backends.linux_sysman import LinuxSysmanBackend
from .backends.wmi_bios import (
    CHARGE_ATTRIBUTE_CANDIDATES,
    THERMAL_ATTRIBUTE_CANDIDATES,
    WmiBiosBackend,
    current_value,
    find_attribute,
    normalise_attribute_list,
    possible_values,
)
from .model import ChargeMode, ThermalProfile
from .service import XpsHelper

OK = "ok"
WARN = "warn"
FAIL = "fail"
INFO = "info"

_STATUS_MARK = {OK: "[ ok ]", WARN: "[warn]", FAIL: "[fail]", INFO: "[info]"}


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    raw: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {"name": self.name, "status": self.status, "detail": self.detail}
        if self.raw:
            data["raw"] = self.raw
        return data


@dataclass
class DoctorReport:
    checks: list[Check] = field(default_factory=list)
    raw_sections: dict[str, str] = field(default_factory=dict)

    def add(self, name: str, status: str, detail: str = "", raw: str | None = None) -> None:
        self.checks.append(Check(name, status, detail, raw))

    @property
    def worst_status(self) -> str:
        for status in (FAIL, WARN):
            if any(check.status == status for check in self.checks):
                return status
        return OK

    @property
    def has_failures(self) -> bool:
        return any(check.status == FAIL for check in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "worst_status": self.worst_status,
            "checks": [check.to_dict() for check in self.checks],
            "raw_sections": self.raw_sections,
        }

    def render(self) -> str:
        lines: list[str] = []
        lines.append(f"{C.APP_DISPLAY_NAME} doctor report")
        lines.append("=" * 66)
        for check in self.checks:
            mark = _STATUS_MARK.get(check.status, "[    ]")
            lines.append(f"{mark} {check.name}")
            if check.detail:
                for chunk in _wrap(check.detail, 62):
                    lines.append(f"       {chunk}")
            if check.raw:
                for chunk in check.raw.strip().splitlines()[:12]:
                    lines.append(f"       | {chunk.strip()[:100]}")
        if self.raw_sections:
            lines.append("")
            lines.append("Raw output (paste this if you report a problem)")
            lines.append("-" * 66)
            for title, body in self.raw_sections.items():
                lines.append(f"--- {title} ---")
                lines.extend(body.strip().splitlines()[:25] or ["(empty)"])
        lines.append("")
        return "\n".join(lines)


def _wrap(text: str, width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= width:
            current += " " + word
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


def run_doctor(service: XpsHelper, *, include_raw: bool = True) -> DoctorReport:
    report = DoctorReport()

    # -- platform ---------------------------------------------------------
    vendor = platform_info.system_vendor()
    model = platform_info.system_model()
    bios = platform_info.bios_version()
    report.add(
        "Platform",
        INFO,
        f"OS: {sys.platform} | Vendor: {vendor or 'unknown'} | Model: {model or 'unknown'}"
        + (f" | BIOS: {bios}" if bios else ""),
    )

    dell = service.is_dell
    if dell is True:
        status = OK if platform_info.is_known_xps(model) else INFO
        detail = "Detected a Dell system."
        if platform_info.is_known_xps(model):
            detail += " This is a model XPS Helper was designed for."
        report.add("Dell platform", status, detail)
    elif dell is False:
        report.add(
            "Dell platform",
            WARN,
            "This does not look like a Dell machine. Dell's BIOS attributes will "
            "not exist; the app will run in monitoring-only mode.",
        )
    else:
        report.add("Dell platform", WARN, "Could not determine the system vendor.")

    # -- control backend --------------------------------------------------
    control = service.control
    caps = service.capabilities(refresh=True)

    if isinstance(control, CompositeControlBackend):
        selected = control.selected()
        report.add(
            "Control backend",
            INFO,
            f"{control.name} -> using {getattr(selected, 'name', 'nothing')}",
        )
    else:
        selected = control
        report.add("Control backend", INFO, f"{getattr(control, 'name', 'unknown')}")

    if isinstance(selected, WmiBiosBackend):
        _doctor_wmi_bios(report, selected, include_raw)
    elif isinstance(selected, LinuxSysmanBackend):
        _doctor_linux_sysman(report, selected)
    elif isinstance(selected, CctkBackend):
        if selected.available:
            report.add("Dell Command | Configure", OK, f"Found at {selected.path}")
            version = selected.version()
            report.add(
                "cctk version", INFO if version else WARN,
                version or "Could not read version.",
            )
            _probe_attribute(
                report, selected, C.ATTR_THERMAL, "Thermal profiles",
                [p.value for p in ThermalProfile], include_raw,
            )
            _probe_attribute(
                report, selected, C.ATTR_PRIMARY_BATT, "Charge modes",
                [m.value for m in ChargeMode], include_raw,
            )
        else:
            report.add(
                "Dell Command | Configure",
                FAIL,
                "cctk was requested or selected but was not found on this system. "
                "Note that cctk is optional: the native firmware interface is the "
                "default path and needs no Dell software.",
            )
    elif selected is None or isinstance(selected, NullControlBackend):
        report.add(
            "BIOS control",
            FAIL,
            "No interface for changing BIOS settings was found. "
            + " ".join(str(note) for note in caps.notes[:3]),
        )

    if selected is not None and hasattr(selected, "is_elevated"):
        elevated = selected.is_elevated()
        report.add(
            "Privileges",
            OK if elevated else WARN,
            "Running elevated." if elevated
            else "Not elevated. Reading works, but *writing* BIOS settings needs "
                 "administrator rights (or sudo on Linux).",
        )

    # Reading the current values also proves the read path round-trips.
    if caps.can_control:
        try:
            profile = control.read_profile()
            report.add(
                "Current thermal profile",
                OK if profile else WARN,
                profile.label if profile else "Could not read the current thermal profile.",
            )
        except Exception as exc:  # noqa: BLE001
            report.add("Current thermal profile", WARN, f"Could not read it: {exc}")
        try:
            charge = control.read_charge_config()
            report.add(
                "Current charge configuration",
                OK if charge else WARN,
                charge.describe if charge else "Could not read the current charge configuration.",
            )
        except Exception as exc:  # noqa: BLE001
            report.add("Current charge configuration", WARN, f"Could not read it: {exc}")
    # -- telemetry --------------------------------------------------------
    battery = None
    try:
        battery = service.telemetry.read_battery()
    except Exception as exc:  # noqa: BLE001
        report.add("Battery", FAIL, f"Read failed: {exc}")
    if battery is not None:
        bits = [f"{battery.percent}%"] if battery.percent is not None else []
        if battery.plugged_in is not None:
            bits.append("on AC" if battery.plugged_in else "on battery")
        if battery.health_percent is not None:
            bits.append(f"health {battery.health_percent}%")
        if battery.full_capacity_mwh and battery.design_capacity_mwh:
            bits.append(f"{battery.full_capacity_mwh}/{battery.design_capacity_mwh} mWh")
        report.add("Battery", OK, ", ".join(bits) or "Battery detected.", )
    elif battery is None:
        report.add("Battery", WARN, "No battery information available.")

    temps = []
    try:
        temps = service.telemetry.read_temperatures()
    except Exception as exc:  # noqa: BLE001
        report.add("Temperatures", FAIL, f"Read failed: {exc}")
    if temps:
        report.add(
            "Temperatures",
            OK,
            " | ".join(f"{t.name}: {t.celsius:.1f}C" for t in temps[:5]),
        )
    else:
        report.add(
            "Temperatures",
            WARN,
            "No temperature sensors found. On Windows, install Dell Command | Monitor "
            "or run LibreHardwareMonitor for CPU package temperatures.",
        )

    fans = []
    try:
        fans = service.telemetry.read_fans()
    except Exception as exc:  # noqa: BLE001
        report.add("Fan speeds", FAIL, f"Read failed: {exc}")
    if fans:
        report.add("Fan speeds", OK, " | ".join(f"{f.name}: {f.rpm} RPM" for f in fans[:4]))
    else:
        report.add(
            "Fan speeds",
            WARN,
            "No fan sensors. Reading RPM needs Dell Command | Monitor. Note that "
            "*manual* fan control is not possible on an XPS 13: the fan is owned by "
            "the EC/BIOS.",
        )

    report.add("Telemetry backend", INFO, f"{getattr(service.telemetry, 'name', '?')}")

    # -- config -----------------------------------------------------------
    try:
        C.config_dir().mkdir(parents=True, exist_ok=True)
        report.add("Config directory", OK, str(C.config_dir()))
    except OSError as exc:
        # Not a failure: the app still monitors and controls, it just cannot
        # remember settings between runs.
        report.add(
            "Config directory",
            WARN,
            f"Not writable ({exc}). Settings will not persist between runs; "
            "everything else still works.",
        )

    # -- verdict ----------------------------------------------------------
    notes = caps.notes
    if notes:
        report.add("Capability notes", INFO, " | ".join(str(n) for n in notes[:6]))

    if service.capabilities().can_control:
        report.add("Verdict", OK, "Thermal and/or charge control is available.")
    else:
        report.add(
            "Verdict",
            FAIL,
            "Control is unavailable. Install Dell Command | Configure and run the app "
            "as administrator. Monitoring still works.",
        )

    return report


def _doctor_wmi_bios(report: DoctorReport, backend: "WmiBiosBackend", include_raw: bool) -> None:
    """Report on the firmware-provided WMI-ACPI BIOS attribute interface."""
    data = backend.refresh()

    if not data.get("interface_found"):
        message = (
            f"The BIOS did not expose {C.WMI_BIOS_INTERFACE_CLASS} in "
            f"{C.WMI_BIOS_NAMESPACE}. Either this model predates WMI-ACPI support, "
            "or the interface is disabled in BIOS setup."
        )
        if data.get("error"):
            message += f" BIOS said: {data['error']}"
        report.add("Native BIOS interface (WMI-ACPI)", FAIL, message)
        return

    report.add(
        "Native BIOS interface (WMI-ACPI)",
        OK,
        f"Found {C.WMI_BIOS_INTERFACE_CLASS} in {C.WMI_BIOS_NAMESPACE}. "
        "No Dell software is required for this path.",
    )

    password_set = data.get("admin_password_set")
    if password_set is True:
        report.add(
            "BIOS administrator password",
            WARN,
            "A password is set, so writes will be denied until it is supplied "
            f"in the app or the {C.ENV_BIOS_PASSWORD} environment variable.",
        )
    elif password_set is False:
        report.add("BIOS administrator password", OK, "No administrator password is set.")

    entries = normalise_attribute_list(data.get("attributes"))
    if not entries:
        report.add(
            "BIOS attributes",
            WARN,
            "The interface exists but none of the expected attributes were returned.",
        )

    for title, candidates in (
        ("Thermal profiles", THERMAL_ATTRIBUTE_CANDIDATES),
        ("Charge modes", CHARGE_ATTRIBUTE_CANDIDATES),
    ):
        entry = find_attribute(entries, candidates)
        if entry is None:
            report.add(title, WARN, "This BIOS does not expose the expected attribute.")
            continue
        values = possible_values(entry)
        detail = (
            f"{entry.get('AttributeName')}: supported = "
            f"{', '.join(values) if values else 'not reported'}"
        )
        current = current_value(entry)
        if current:
            detail += f" | current = {current}"
        report.add(title, OK if values else WARN, detail)
        if include_raw and not values:
            # The property name may differ on this BIOS revision; dump it whole
            # so the parser can be corrected from a real report.
            report.raw_sections[f"{entry.get('AttributeName')} raw"] = json.dumps(
                entry, indent=2, default=str
            )


def _doctor_linux_sysman(report: DoctorReport, backend: "LinuxSysmanBackend") -> None:
    """Report on the kernel's firmware-attributes sysfs interface."""
    if not backend.available:
        report.add(
            "Native BIOS attributes (sysfs)",
            FAIL,
            f"No BIOS attributes found under {backend.root}. The dell-wmi-sysman "
            "kernel module may not be loaded, or this model may not support "
            "WMI-ACPI BIOS attributes.",
        )
        return

    report.add(
        "Native BIOS attributes (sysfs)",
        OK,
        f"Found under {backend.root}: {', '.join(sorted(backend.attributes))}",
    )

    for title, candidates in (
        ("Thermal profiles", THERMAL_ATTRIBUTE_CANDIDATES),
        ("Charge modes", CHARGE_ATTRIBUTE_CANDIDATES),
    ):
        path = backend.find_attribute_dir(candidates)
        if path is None:
            report.add(title, WARN, "This machine does not expose the expected attribute.")
            continue
        values = backend.possible_values_for(path)
        current = backend.current_value_for(path)
        detail = (
            f"{path.name}: supported = {', '.join(values) if values else 'not reported'}"
        )
        if current:
            detail += f" | current = {current}"
        report.add(title, OK if values else WARN, detail)


def _probe_attribute(
    report: DoctorReport,
    control: CctkBackend,
    attribute: str,
    title: str,
    documented: list[str],
    include_raw: bool,
) -> None:
    values, raw = control.supported_values(attribute)
    if not values:
        report.add(
            f"{title} (--{attribute})",
            WARN,
            "Could not parse Dell's supported-value list. Treating all documented "
            f"values as available: {', '.join(documented)}.",
            raw=raw if include_raw else None,
        )
        if include_raw and raw.strip():
            report.raw_sections[f"cctk -H --{attribute}"] = raw
        return

    supported = [token for token, ok in values.items() if ok]
    unsupported = [token for token, ok in values.items() if not ok]
    detail = f"Supported: {', '.join(supported) or 'none reported'}"
    if unsupported:
        detail += f" | Reported unsupported: {', '.join(unsupported)}"
    report.add(f"{title} (--{attribute})", OK if supported else FAIL, detail)
