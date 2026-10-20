"""Dell-specific constants: attribute names, executable locations, exit codes.

Warning to future maintainers
-----------------------------
The literal strings in this module are *firmware interface tokens*, not styling
choices. They were extracted from hardware-tested GPL-3.0 sources (see
CREDITS.md) rather than inferred from Dell's marketing names, because those two
diverge. For example the CLI token is ``UltraPerformance`` (no space) and the
"primarily AC use" battery mode is ``PrimAcUse``. Renaming one of these breaks
the feature silently on real hardware, so they are asserted by tests.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "xps-helper"
APP_DISPLAY_NAME = "XPS Helper"
APP_VERSION = "0.6.2"

# --------------------------------------------------------------------------
# Dell Command | Configure (cctk)
# --------------------------------------------------------------------------
# Windows: the real x86_64 payload lands under Program Files (x86) even on
# 64-bit Windows because the installer is 32-bit. We probe several layouts
# because Dell's installer has moved files between releases.
CCTK_WINDOWS_CANDIDATES = (
    r"%ProgramFiles(x86)%\Dell\Command Configure\X86_64\cctk.exe",
    r"%ProgramFiles%\Dell\Command Configure\X86_64\cctk.exe",
    r"%ProgramFiles(x86)%\Dell\Command Configure\cctk.exe",
    r"%ProgramFiles%\Dell\Command Configure\cctk.exe",
)
CCTK_LINUX_CANDIDATES = (
    "/opt/dell/dcc/cctk",
    "/usr/bin/cctk",
    "/usr/sbin/cctk",
)


def cctk_candidates() -> tuple[Path, ...]:
    """Return candidate cctk paths for the current platform, env-expanded."""
    raw = CCTK_WINDOWS_CANDIDATES if sys.platform == "win32" else CCTK_LINUX_CANDIDATES
    return tuple(Path(os.path.expandvars(p)) for p in raw)


# --------------------------------------------------------------------------
# BIOS attribute names (as accepted by `cctk --<Attribute>`)
# --------------------------------------------------------------------------
ATTR_THERMAL = "ThermalManagement"
ATTR_PRIMARY_BATT = "PrimaryBattChargeCfg"
ATTR_ADV_BATT = "AdvBatteryChargeCfg"

#: Attributes whose supported values we probe to build a capability report.
PROBED_ATTRIBUTES = (ATTR_THERMAL, ATTR_PRIMARY_BATT, ATTR_ADV_BATT)

# --------------------------------------------------------------------------
# Custom charge-threshold limits, enforced by the firmware itself.
# Quoted from Dell's own documentation, confirmed by dell-powermanager.
# --------------------------------------------------------------------------
CUSTOM_START_MIN = 50
CUSTOM_START_MAX = 95
CUSTOM_STOP_MIN = 55
CUSTOM_STOP_MAX = 100
CUSTOM_MIN_GAP = 5

# --------------------------------------------------------------------------
# cctk exit codes. Anything not listed here is reported verbatim.
# --------------------------------------------------------------------------
CCTK_EXIT_OK = 0
CCTK_EXIT_SETUP_PWD_INVALID = 58
CCTK_EXIT_SETUP_PWD_REQUIRED = 65
CCTK_EXIT_SYS_PWD_REQUIRED = 66
CCTK_EXIT_SYS_PWD_INVALID = 67
CCTK_EXIT_OWNER_PWD_SET = 112

CCTK_PWD_REQUIRED_CODES = frozenset(
    {CCTK_EXIT_SETUP_PWD_REQUIRED, CCTK_EXIT_SYS_PWD_REQUIRED}
)
CCTK_PWD_INVALID_CODES = frozenset(
    {CCTK_EXIT_SETUP_PWD_INVALID, CCTK_EXIT_SYS_PWD_INVALID}
)

#: BIOS password is injected as an argv element, never through a shell, so the
#: password never appears in a command line that a shell could re-expand.
BIOS_PWD_ARG_SETUP = "--ValSetupPwd"
BIOS_PWD_ARG_SYS = "--ValSysPwd"

#: Environment variable read at startup so the password is not stored in the
#: app's config file.
ENV_BIOS_PASSWORD = "XPSHELPER_BIOS_PWD"
ENV_DEBUG = "XPSHELPER_DEBUG"

# --------------------------------------------------------------------------
# Windows WMI surfaces used for read-only telemetry.
# --------------------------------------------------------------------------
#: Dell Command | Monitor namespace: fan RPM + temperature probes.
WMI_NS_DCIM = "root/dcim/sysman"
WMI_CLASS_NUMERIC_SENSOR = "DCIM_NumericSensor"

#: LibreHardwareMonitor's WMI provider, if the user runs it (best CPU package temp).
WMI_NS_LHM = "root/LibreHardwareMonitor"
WMI_CLASS_LHM_SENSOR = "Sensor"

#: Last-resort ACPI thermal zone (tenths of a Kelvin).
WMI_NS_ACPI = "root/wmi"
WMI_CLASS_THERMAL_ZONE = "MSAcpi_ThermalZoneTemperature"

#: Standard battery classes.
WMI_NS_CIMV2 = "root/cimv2"
WMI_CLASS_BATTERY = "Win32_Battery"

# --------------------------------------------------------------------------
# Native Dell BIOS-attribute interface (WMI-ACPI) -- no Dell software required.
#
# The BIOS itself exposes this via ACPI-WMI (PNP0C14) on Dell machines built
# from ~2018 onward. It is the same interface the Linux kernel drives through
# dell-wmi-sysman. Namespace/class/method names and the status-code table come
# from Dell's "Agentless Client Manageability" documentation and the Dell-authored
# sample scripts listed in CREDITS.md.
# --------------------------------------------------------------------------
WMI_BIOS_NAMESPACE = r"root\dcim\sysman\biosattributes"
WMI_BIOS_INTERFACE_CLASS = "BIOSAttributeInterface"
WMI_BIOS_INTERFACE_METHOD = "SetAttribute"

#: Attribute classes hold different value types. ThermalManagement and
#: PrimaryBattChargeCfg are enumerations; we query the others too so the generic
#: discovery works on any model.
WMI_BIOS_ATTRIBUTE_CLASSES = (
    "EnumerationAttribute",
    "IntegerAttribute",
    "StringAttribute",
    "PassobjAttribute",
)

WMI_SECURITY_NAMESPACE = r"root\dcim\sysman\wmisecurity"
WMI_SECURITY_PASSWORD_CLASS = "PasswordObject"
WMI_SECURITY_ADMIN_ID = "Admin"

#: Return codes from BIOSAttributeInterface.SetAttribute.
WMI_BIOS_STATUS_OK = 0
WMI_BIOS_STATUS_FAILED = 1
WMI_BIOS_STATUS_INVALID_PARAMETER = 2
WMI_BIOS_STATUS_ACCESS_DENIED = 3
WMI_BIOS_STATUS_NOT_SUPPORTED = 4
WMI_BIOS_STATUS_MEMORY_ERROR = 5
WMI_BIOS_STATUS_PROTOCOL_ERROR = 6

WMI_BIOS_STATUS_TEXT = {
    WMI_BIOS_STATUS_OK: "Success",
    WMI_BIOS_STATUS_FAILED: "Failed",
    WMI_BIOS_STATUS_INVALID_PARAMETER: "Invalid parameter",
    WMI_BIOS_STATUS_ACCESS_DENIED: "Access denied",
    WMI_BIOS_STATUS_NOT_SUPPORTED: "Not supported",
    WMI_BIOS_STATUS_MEMORY_ERROR: "Memory error",
    WMI_BIOS_STATUS_PROTOCOL_ERROR: "Protocol error",
}

#: Security type for a plain-text administrator password in the SetAttribute call.
WMI_BIOS_SEC_TYPE_PASSWORD = 1

# --------------------------------------------------------------------------
# Linux: the same BIOS attributes exposed through sysfs by dell-wmi-sysman.
# --------------------------------------------------------------------------
LINUX_FIRMWARE_ATTRIBUTES = "/sys/class/firmware-attributes"

# --------------------------------------------------------------------------
# Dell platform detection.
# --------------------------------------------------------------------------
DELL_MANUFACTURER_HINTS = ("dell", "dell inc.")

#: Models this project was designed for; used only for friendly messaging.
KNOWN_XPS_MODELS = ("XPS 13 7390", "XPS 13 7390 2-in-1")


def config_dir() -> Path:
    """Per-user config directory, honouring platform conventions."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~\\AppData\\Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / APP_NAME


def config_file() -> Path:
    return config_dir() / "config.json"


def log_file() -> Path:
    return config_dir() / "xps-helper.log"
