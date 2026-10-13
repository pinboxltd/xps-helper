"""Backend selection and construction.

Priority order for ``auto``
---------------------------
1. **Native firmware interface** -- WMI-ACPI BIOS attributes on Windows, the
   kernel's ``firmware-attributes`` sysfs class on Linux. This is the default
   because it requires no Dell software to be installed.
2. **Dell Command | Configure** (``cctk``) -- only used if the user already has
   it, or explicitly asks for it. It is no longer required.
3. Read-only monitoring with an explanation of what was missing.

Two capabilities are separated on purpose: *control* (writing thermal profile and
charge configuration) and *telemetry* (reading battery, temperatures, fans). A
machine can support one without the other, so the service composes them.
"""

from __future__ import annotations

import sys

from ..model import Capabilities
from .base import (
    ControlBackend,
    NullControlBackend,
    NullTelemetryBackend,
    TelemetryBackend,
)
from .cctk import CctkBackend
from .composite import CompositeControlBackend
from .linux import LinuxTelemetryBackend
from .linux_sysman import LinuxSysmanBackend
from .mock import MockBackend
from .wmi import WmiTelemetryBackend
from .wmi_bios import WmiBiosBackend

__all__ = [
    "ControlBackend",
    "TelemetryBackend",
    "NullControlBackend",
    "NullTelemetryBackend",
    "CctkBackend",
    "CompositeControlBackend",
    "LinuxSysmanBackend",
    "MockBackend",
    "WmiBiosBackend",
    "WmiTelemetryBackend",
    "LinuxTelemetryBackend",
    "build_backends",
    "native_control_backend",
    "BACKEND_CHOICES",
]

BACKEND_CHOICES = ("auto", "native", "cctk", "mock", "null")


def native_control_backend(*, bios_password: str | None = None):
    """The firmware-native control backend for this platform, if any.

    Returns ``None`` on platforms with no known native interface.
    """
    if sys.platform == "win32":
        return WmiBiosBackend(bios_password=bios_password)
    if sys.platform.startswith("linux"):
        return LinuxSysmanBackend(bios_password=bios_password)
    return None


def _make_control(kind: str, *, bios_password: str | None = None) -> ControlBackend:
    if kind == "mock":
        return MockBackend()
    if kind == "null":
        return NullControlBackend("Control disabled by request (--backend null).")

    if kind == "native":
        native = native_control_backend(bios_password=bios_password)
        if native is None:
            return NullControlBackend(
                f"No native BIOS-attribute interface is known for platform {sys.platform!r}."
            )
        return CompositeControlBackend([native], label="native")

    if kind == "cctk":
        backend = CctkBackend(bios_password=bios_password)
        if backend.available:
            return backend
        return NullControlBackend(
            "cctk was requested but Dell Command | Configure was not found."
        )

    if kind == "auto":
        candidates = []
        native = native_control_backend(bios_password=bios_password)
        if native is not None:
            candidates.append(native)
        # Only a fallback: cctk is no longer required, but if the user already
        # has it, it can succeed on machines whose BIOS lacks WMI-ACPI attributes.
        candidates.append(CctkBackend(bios_password=bios_password))
        if not candidates:
            return NullControlBackend(
                f"No control backend is available for platform {sys.platform!r}."
            )
        return CompositeControlBackend(candidates, label="auto")

    raise ValueError(f"unknown backend kind: {kind!r}")


def _make_telemetry(kind: str) -> TelemetryBackend:
    if kind == "mock":
        return MockBackend()
    if kind == "null":
        return NullTelemetryBackend("Telemetry disabled by request (--backend null).")
    if kind in ("cctk", "native"):
        # Neither is a telemetry interface; use the platform's native reader.
        kind = "auto"
    if kind == "auto":
        if sys.platform == "win32":
            return WmiTelemetryBackend()
        if sys.platform.startswith("linux"):
            return LinuxTelemetryBackend()
        return NullTelemetryBackend(f"No telemetry backend for platform {sys.platform!r}.")
    raise ValueError(f"unknown backend kind: {kind!r}")


def build_backends(
    kind: str = "auto", *, bios_password: str | None = None
) -> tuple[ControlBackend, TelemetryBackend]:
    """Construct the control and telemetry backends for ``kind``.

    ``mock`` returns the same object for both roles so that a write is
    immediately visible in the next read -- which is what makes the mock useful
    for testing the full write/verify/display cycle.
    """
    control = _make_control(kind, bios_password=bios_password)
    telemetry = _make_telemetry(kind)
    if kind == "mock":
        mock = control
        assert isinstance(mock, MockBackend)
        telemetry = mock
    return control, telemetry


def probe_capabilities(control: ControlBackend, telemetry: TelemetryBackend) -> Capabilities:
    """Combine a control probe with telemetry availability into one report."""
    try:
        caps = control.probe()
    except Exception as exc:  # noqa: BLE001 - a failed probe must not be fatal
        caps = Capabilities(control_backend=getattr(control, "name", "unknown"))
        caps.notes.append(f"Capability probe failed: {exc}")

    caps.telemetry_backend = getattr(telemetry, "name", "unknown")
    try:
        caps.battery_readable = telemetry.read_battery() is not None
        caps.temperatures_readable = bool(telemetry.read_temperatures())
        caps.fans_readable = bool(telemetry.read_fans())
    except Exception as exc:  # noqa: BLE001
        caps.notes.append(f"Telemetry probe failed: {exc}")

    try:
        if hasattr(telemetry, "describe"):
            caps.notes.extend(telemetry.describe())
    except Exception:  # noqa: BLE001
        pass
    return caps
