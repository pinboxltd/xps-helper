"""Platform and machine identification.

Used for friendly messaging ("this is an XPS 13 7390") and for the `doctor`
report. Never used to *gate* features: capability probing decides what is
possible, because a model name cannot tell you which BIOS attributes exist.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

from . import constants as C

log = logging.getLogger(__name__)


def _linux_dmi(field: str) -> str | None:
    path = Path(f"/sys/class/dmi/id/{field}")
    try:
        value = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    return value or None


@lru_cache(maxsize=1)
def _windows_system_info() -> tuple[str | None, str | None]:
    """Return (manufacturer, model) on Windows via one CIM query."""
    script = (
        "$c = Get-CimInstance -ClassName Win32_ComputerSystem;"
        "Write-Output ($c.Manufacturer + '|' + $c.Model)"
    )
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=20,
            errors="replace",
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log.debug("system info query failed: %s", exc)
        return (None, None)
    line = (proc.stdout or "").strip().splitlines()
    if not line:
        return (None, None)
    manufacturer, _, model = line[0].partition("|")
    return (manufacturer.strip() or None, model.strip() or None)


def system_vendor() -> str | None:
    if sys.platform == "win32":
        return _windows_system_info()[0]
    return _linux_dmi("sys_vendor")


def system_model() -> str | None:
    if sys.platform == "win32":
        return _windows_system_info()[1]
    return _linux_dmi("product_name")


def bios_version() -> str | None:
    if sys.platform == "win32":
        script = "(Get-CimInstance -ClassName Win32_BIOS).SMBIOSBIOSVersion"
        try:
            proc = subprocess.run(  # noqa: S603
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                text=True,
                timeout=20,
                errors="replace",
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        return (proc.stdout or "").strip() or None
    return _linux_dmi("bios_version")


def is_dell() -> bool | None:
    """True/False when we can tell, None when detection itself failed."""
    vendor = system_vendor()
    if vendor is None:
        return None
    lowered = vendor.lower()
    return any(hint in lowered for hint in C.DELL_MANUFACTURER_HINTS)


def is_known_xps(model: str | None) -> bool:
    if not model:
        return False
    return any(known.lower() in model.lower() for known in C.KNOWN_XPS_MODELS)


def summary() -> str:
    model = system_model() or "unknown model"
    vendor = system_vendor() or "unknown vendor"
    return f"{vendor} {model}".strip()
