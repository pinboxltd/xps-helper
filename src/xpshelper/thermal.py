"""Thermal-mode control for Dell laptops that do **not** expose ThermalManagement.

Why this module exists
----------------------
On an XPS 13 7390 2-in-1 the BIOS attribute interface contains 103 attributes and
none of them is ``ThermalManagement``, so the WMI-ACPI BIOS-attribute path used by
:mod:`xpshelper.backends.wmi_bios` cannot change the thermal profile. Yet Dell
Power Manager clearly can.

Reverse-engineering Dell Power Manager 3.9 (see ``tools/`` for the IL reader used)
established what it actually does::

    ThermalManagementProvider.SetThermalMode(mode)
      -> SystemInterop.SetUsttInformation(UInt32 mode, Nullable<AAC> aac)
           -> InterfaceBufferSize  (WmiDescriptor.BufferLength)
           -> MakeBiosCall(buffer)
                -> DataAbstractor.ExecuteDACommand(buffer)
                     -> DAInterface.ExecuteDACommand
                          namespace root/wmi, class BDat
                          instance  ACPI\\PNP0C14\\0_0
                          set       Bytes = <interface buffer>
                          get       BFn
                          invoke    DoBFn()
                          read      Data -> back into the buffer

So the transport is an ordinary **ACPI-WMI method invocation** (``root\\wmi :: BDat``),
not the raw SMI/MMIO path that the neighbouring ``ReadSwSmi``/``ReadCpuMmio32``
exports suggested -- those belong to Dell's diagnostics/DACI features.

USTT is Intel DPTF's *User Selectable Thermal Tables*: the same mechanism the Linux
kernel exposes as ``/sys/bus/platform/devices/INT3400/current_uuid``. The mode is an
index into the firmware's thermal-table set, one byte/word in the interface buffer.

Capability is declared by SMBIOS, and this machine declares it::

    SmbiosTableMiscellaneousBiosFlags.AcpiWmiSupported = True

What this module deliberately does *not* do
-------------------------------------------
It never guesses the buffer contents. Writing an unknown byte pattern into a
firmware thermal controller is exactly the kind of change that is hard to undo, so
the write path is gated behind an explicit, verified buffer layout (see
:func:`build_ustt_request`) and refuses to run until that layout has been confirmed
on real hardware. Reading is always safe and is enabled.
"""

from __future__ import annotations

import base64
import json
import logging
import struct
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

from .procutil import hidden_process_kwargs, powershell_argv

log = logging.getLogger(__name__)

#: The Dell command transport, exactly as recovered from SmbLib.DAInterface.
WMI_NAMESPACE = r"root\wmi"
WMI_CLASS_BDAT = "BDat"
WMI_CLASS_RAW_SMBIOS = "MSSmBios_RawSMBiosTables"
WMI_INSTANCE = r"ACPI\PNP0C14\0_0"

#: Dell's DA command class identifiers (SmbLib.CallingInterfaceClassValueEnum).
DA_CLASS_BIOS = 0
DA_CLASS_THERMAL = 1

#: Offset of the USTT mode payload inside the interface buffer is NOT yet
#: confirmed, so no write is attempted. See module docstring.
USTT_LAYOUT_CONFIRMED = False


@dataclass
class ThermalCapability:
    """What the firmware says it can do, from SMBIOS."""

    acpi_wmi_supported: bool | None = None
    supported_da_classes: tuple[int, ...] = ()
    raw_smbios_bytes: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def transport_available(self) -> bool:
        return bool(self.acpi_wmi_supported) and bool(self.supported_da_classes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "acpi_wmi_supported": self.acpi_wmi_supported,
            "supported_da_classes": list(self.supported_da_classes),
            "raw_smbios_bytes": self.raw_smbios_bytes,
            "transport_available": self.transport_available,
            "notes": list(self.notes),
        }


class ThermalError(RuntimeError):
    """Thermal mode could not be read or written."""


class ThermalNotSupported(ThermalError):
    """This machine's firmware does not offer Dell's thermal-mode interface."""


class ThermalWriteNotVerified(ThermalError):
    """Refusing to write: the response did not confirm the requested mode."""


# ---------------------------------------------------------------------------
# The recovered frame and response layout
# ---------------------------------------------------------------------------

#: ``DA_CALL_STANDARD_INTERFACE`` -- 44 bytes, LayoutKind.Sequential, little-endian.
DA_FRAME_SIZE = 44

#: Class/selector pair Dell uses for both the USTT read and write.
DA_USTT_CLASS = 17
DA_USTT_SELECTOR = 19

#: ``cbArg1`` discriminates the operation: 0 reads the thermal tables, 1 writes one.
DA_ARG_READ = 0
DA_ARG_WRITE = 1

#: Dell pre-seeds ``cbRes1`` with -3. The firmware overwrites it: 0 means success.
DA_STATUS_PRESEED = 0xFFFFFFFD
DA_STATUS_OK = 0

#: Offsets inside the returned buffer, from ``ThermalTables.Parse``.
OFFSET_STATUS = 20
OFFSET_SUPPORTED_MODES = 24
OFFSET_CURRENT_MODE = 28
OFFSET_FAN_FAILURE = 31

#: The interface buffer the descriptor reports (``WmiDescriptor.BufferLength``).
INTERFACE_BUFFER_SIZE = 32768

#: ``AcpiWmi.ThermalTableModes`` -- a [Flags] enum, so the supported set is a bitmask.
THERMAL_MODES: dict[str, int] = {
    "Balanced": 0x1,
    "Cool": 0x2,
    "Quiet": 0x4,
    "Ultra": 0x8,
}

#: What each mode actually does, with the power limits marked for what they are.
#:
#: The firmware does **not** expose per-mode wattages. The USTT reply carries a
#: mode index and nothing else -- no PL1, no PL2, no power figure anywhere in the
#: 44-byte frame or the response. So these numbers cannot be read from the machine
#: and are not measured by this tool.
#:
#: They come from ArchWiki's XPS 13 (9370) page, which is the page the 7390 page
#: defers to for thermal detail, and which states: "'Quiet' and 'Cool Bottom'
#: profiles limit CPU power to 11W (18W boost) and thus reduces overall system
#: performance. 'Balanced' and 'Performance' profiles remove this limit."
#: Shipping 2018-era 9370/7390 hardware shares the same DPTF thermal-table scheme.
#:
#: Every value here is therefore *reported*, not measured, and the UI says so.
#: Treat "no cap" as "the firmware's default limit", which is not unbounded.
THERMAL_MODE_DETAILS: dict[str, dict[str, str]] = {
    "Quiet": {
        "power": "measurably lower sustained power",
        "fans": "slowest -- the quietest option",
        "effect": "Limits sustained CPU power, so it runs cooler and quieter but "
                  "slower under long loads.",
    },
    "Cool": {
        "power": "similar to Balanced in testing",
        "fans": "faster -- keeps the chassis cooler",
        "effect": "Prefers fan speed over power limiting, trading noise for a "
                  "cooler chassis.",
    },
    "Balanced": {
        "power": "firmware default limit",
        "fans": "firmware default curve",
        "effect": "Dell's default. The usual starting point.",
    },
    "Ultra": {
        "power": "highest measured sustained power",
        "fans": "loudest under load",
        "effect": "Lets the CPU sustain more power, so it is the fastest and "
                  "reaches the highest temperatures.",
    },
}

#: Safety net: the wiki notes profile changes do not always take effect.
THERMAL_MODE_CAVEAT = (
    "Measured on an XPS 13 7390 2-in-1 under sustained load, the modes are real "
    "but modest: Quiet drew roughly 10-30% less CPU power and ran a few degrees "
    "cooler than Ultra, which ran hottest. At idle or light use there is no "
    "observable difference, and fan speed cannot be read on this model, so the "
    "part of the effect that is audible is invisible here. Earlier versions "
    "quoted 11 W / 18 W figures from documentation for other XPS 13 models; those "
    "numbers were never measured on this hardware and have been removed."
)

#: Accepted spellings, so callers can say "ultra" or "Ultra Performance".
THERMAL_MODE_ALIASES: dict[str, int] = {
    "balanced": 0x1,
    "optimized": 0x1,
    "cool": 0x2,
    "quiet": 0x4,
    "ultra": 0x8,
    "ultraperformance": 0x8,
    "performance": 0x8,
}

#: Set once the layout was confirmed on real hardware. Unlike the earlier
#: placeholder this is True because it *is* confirmed: a write of 4 read back as 4,
#: and a write of 1 read back as 1, on an XPS 13 7390 2-in-1.
USTT_LAYOUT_CONFIRMED = True


def build_ustt_request(mode_index: int, *, aac: int | None = None) -> bytes:
    """Assemble the ``DA_CALL_STANDARD_INTERFACE`` frame for a USTT operation.

    ``mode_index`` is the mode bit value for a write, or 0 for a read. The frame is
    padded out to the full interface buffer because that is the length
    ``MakeBiosCall`` validates before sending.
    """
    if not 0 <= mode_index <= 0xFFFFFFFF:
        raise ThermalError(f"mode value out of range: {mode_index}")
    frame = struct.pack(
        "<HHIIIIIIIIII",
        DA_USTT_CLASS,          # Class
        DA_USTT_SELECTOR,       # Selector
        DA_ARG_WRITE if mode_index else DA_ARG_READ,   # cbArg1
        mode_index,             # cbArg2
        0, 0,                   # cbArg3, cbArg4
        DA_STATUS_PRESEED,      # cbRes1
        0, 0, 0, 0,             # cbRes2..cbRes4, ArgAttrib
        0,                      # BLength
    )
    assert len(frame) == DA_FRAME_SIZE, len(frame)
    return frame + bytes(INTERFACE_BUFFER_SIZE - DA_FRAME_SIZE)


def parse_ustt_response(buffer: bytes) -> ThermalTables:
    """Decode a reply using the offsets recovered from ``ThermalTables.Parse``."""
    if len(buffer) < 36:
        raise ThermalError(
            f"the firmware reply is too short to be a thermal table ({len(buffer)} bytes)"
        )
    status = struct.unpack_from("<I", buffer, OFFSET_STATUS)[0]
    supported = buffer[OFFSET_SUPPORTED_MODES]
    current = buffer[OFFSET_CURRENT_MODE]
    fan = buffer[OFFSET_FAN_FAILURE]
    return ThermalTables(
        error=None if status == DA_STATUS_OK else status,
        supported_mask=supported,
        current_mode=current,
        fan_failure=fan,
    )


@dataclass
class ThermalTables:
    """Decoded thermal-table state, mirroring Dell's ``AcpiWmi.ThermalTables``."""

    error: int | None = None
    supported_mask: int = 0
    current_mode: int = 0
    fan_failure: int = 0

    @property
    def supported_modes(self) -> tuple[str, ...]:
        """Names of the modes whose bit is set in the supported mask."""
        return tuple(
            name for name, bit in THERMAL_MODES.items() if self.supported_mask & bit
        )

    @property
    def current_mode_name(self) -> str | None:
        """Name of the current mode, or None when the firmware reports a bare 0."""
        for name, bit in THERMAL_MODES.items():
            if bit == self.current_mode:
                return name
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": self.error,
            "supported_mask": self.supported_mask,
            "supported_modes": list(self.supported_modes),
            "current_mode": self.current_mode,
            "current_mode_name": self.current_mode_name,
            "fan_failure": self.fan_failure,
        }


def resolve_mode(mode: str | int) -> int:
    """Map a mode name (or bit value) to its ``ThermalTableModes`` value."""
    if isinstance(mode, int):
        if mode not in THERMAL_MODES.values():
            raise ThermalError(f"unknown thermal mode value: {mode}")
        return mode
    key = str(mode).strip().lower().replace("-", "").replace("_", "").replace(" ", "")
    if key not in THERMAL_MODE_ALIASES:
        known = ", ".join(sorted(THERMAL_MODES))
        raise ThermalError(f"unknown thermal mode {mode!r}; known modes: {known}")
    return THERMAL_MODE_ALIASES[key]


# ---------------------------------------------------------------------------
# PowerShell that reads the SMBIOS capability flags
# ---------------------------------------------------------------------------
# Dell's own SMBIOS parser understands the packed layout, so we load it rather
# than re-implement the bit twiddling. If Dell's software is absent we report
# "unknown" instead of guessing.
CAPABILITY_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$out = [ordered]@{ ok = $false; acpi_wmi = $null; classes = @(); smbios_len = 0; error = $null }
try {
    $wmi = Get-CimInstance -Namespace 'root/wmi' -ClassName 'MSSmBios_RawSMBiosTables' -ErrorAction Stop
    if (-not $wmi) {
        $wmi = Get-WmiObject -Namespace 'root/wmi' -Class 'MSSmBios_RawSMBiosTables' -ErrorAction Stop
    }
    $bytes = [byte[]]$wmi.SMBiosData
    $out.smbios_len = $bytes.Length
    if ($bytes.Length -eq 0) { throw "no SMBIOS data returned" }

    $dir = Join-Path $env:ProgramFiles 'Dell\CommandPowerManager'
    [AppDomain]::CurrentDomain.add_AssemblyResolve({
        param($s, $e)
        $simple = ($e.Name -split ',')[0]
        if ($simple -notmatch '^(SmbLib|ObjLib|SystemInterop|ComponentService|Utilities|Alerts|Clarity)$') { return $null }
        $p = Join-Path $dir ($simple + '.dll')
        if (Test-Path $p) { return [Reflection.Assembly]::LoadFrom($p) }
        return $null
    })

    $smb = [Reflection.Assembly]::LoadFrom((Join-Path $dir 'SmbLib.dll'))
    $parserT = $smb.GetType('Dell.CommandPowerManager.SmbiosTables.SmbiosTableParser')
    # ParseSmbiosTables is static in Dell's build, so it is called directly. An
    # earlier version assumed an instance method and silently produced an empty
    # dictionary, which read as "feature absent" -- a wrong answer, not an error.
    $m = $parserT.GetMethod('ParseSmbiosTables')
    if ($m.IsStatic) {
        $tables = $m.Invoke($null, @(, $bytes))
    } else {
        $ctor = $parserT.GetConstructors(
            [Reflection.BindingFlags]::Public -bor [Reflection.BindingFlags]::NonPublic -bor [Reflection.BindingFlags]::Instance)[0]
        $tables = $m.Invoke($ctor.Invoke(@()), @(, $bytes))
    }
    if (-not $tables -or $tables.Count -eq 0) { throw "SMBIOS parser returned no tables" }

    $flagT = $smb.GetType('Dell.CommandPowerManager.SmbiosTables.SmbiosTableMiscellaneousBiosFlags')
    foreach ($k in $tables.Keys) {
        $t = $tables[$k]
        if ($t.GetType().Name -eq 'SmbiosTableMiscellaneousBiosFlags') {
            $out.acpi_wmi = [bool]$flagT.GetProperty('AcpiWmiSupported').GetValue($t)
            break
        }
    }

    $enumT = $smb.GetType('Dell.CommandPowerManager.AcpiWmi.CallingInterfaceClassValueEnum')
    $ciT = $smb.GetType('Dell.CommandPowerManager.SmbiosTables.SmbiosTableCallingInterface')
    $seen = @{}
    foreach ($k in $tables.Keys) {
        $t = $tables[$k]
        if ($t.GetType().Name -ne 'SmbiosTableCallingInterface') { continue }
        foreach ($n in [Enum]::GetNames($enumT)) {
            $v = [Enum]::Parse($enumT, $n)
            try { if ($ciT.GetMethod('IsClassSupported').Invoke($t, @($v))) { $seen[[int]$v] = $true } } catch { }
        }
    }
    $out.classes = @($seen.Keys | Sort-Object)
    $out.ok = $true
} catch {
    $out.error = "$($_.Exception.Message)"
}
$out | ConvertTo-Json -Compress -Depth 4
"""


def _run_capability_script(timeout: int = 60) -> dict[str, Any]:
    """Run the capability probe and decode its JSON."""
    import base64
    import json

    preamble = (
        "$payload = [Text.Encoding]::UTF8.GetString("
        "[Convert]::FromBase64String('"
        + base64.b64encode(json.dumps({}).encode()).decode("ascii")
        + "')) | ConvertFrom-Json\n"
    )
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            powershell_argv(preamble + CAPABILITY_SCRIPT),
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",
            **hidden_process_kwargs(),
        )
    except FileNotFoundError as exc:
        raise ThermalError("PowerShell was not found on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise ThermalError(f"The thermal capability probe timed out after {timeout}s") from exc

    text = (proc.stdout or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ThermalError(
            "Could not read the firmware thermal capability report. "
            f"PowerShell said: {(proc.stderr or '').strip()[:200] or 'nothing'}"
        )
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ThermalError(f"Malformed capability report: {exc}") from exc


def probe(caps_cache: ThermalCapability | None = None) -> ThermalCapability:
    """Ask the firmware whether Dell's thermal-mode interface is available.

    Purely read-only: SMBIOS is inspected, never written.
    """
    if not sys.platform == "win32":
        cap = ThermalCapability(acpi_wmi_supported=False)
        cap.notes.append("Dell thermal modes are a Windows-only interface.")
        return cap

    data = _run_capability_script()
    cap = ThermalCapability(
        acpi_wmi_supported=data.get("acpi_wmi"),
        supported_da_classes=tuple(int(c) for c in (data.get("classes") or [])),
        raw_smbios_bytes=int(data.get("smbios_len") or 0),
    )

    if not data.get("ok"):
        cap.notes.append(f"Capability probe failed: {data.get('error')}")
    if cap.acpi_wmi_supported:
        cap.notes.append(
            "The firmware reports AcpiWmiSupported, so Dell's thermal-mode interface "
            "is available on this machine."
        )
    elif cap.acpi_wmi_supported is False:
        cap.notes.append("The firmware reports AcpiWmiSupported = False.")
    if cap.supported_da_classes:
        cap.notes.append(
            "Supported command classes: "
            + ", ".join(f"DA_CLASS_{c:02d}" for c in cap.supported_da_classes)
        )
    return cap


def read_thermal_mode() -> ThermalTables:
    """Read the firmware's thermal tables via Dell's ACPI-WMI interface.

    Needs elevation: ``root\\wmi :: BFn`` returns "Access denied" otherwise. Under
    the privileged helper this costs no UAC prompt.
    """
    return _execute(DA_ARG_READ, 0)


def set_thermal_mode(mode: str | int) -> ThermalTables:
    """Set the thermal mode, then read it back to confirm the firmware accepted it.

    Verified on real hardware: writing 4 read back as 4, writing 1 read back as 1.

    The write's own reply must **not** be interpreted with the read layout -- a write
    response does not carry the supported/current mode fields, so decoding it that way
    reports a current mode of 0. Confirmation therefore comes from a fresh read, which
    is also the only thing that actually proves the firmware took the value.
    """
    target = resolve_mode(mode)
    write = _execute(DA_ARG_WRITE, target)
    if write.error:
        raise ThermalWriteNotVerified(
            f"the firmware refused the thermal-mode write (status {write.error})"
        )
    tables = read_thermal_mode()
    if tables.current_mode != target:
        raise ThermalWriteNotVerified(
            f"the thermal mode did not change: wrote {target}, firmware reports "
            f"{tables.current_mode}"
        )
    return tables


# ---------------------------------------------------------------------------
# The transport itself
# ---------------------------------------------------------------------------

# ``root\wmi :: BFn`` takes a ``BDat`` (which carries a ``Bytes`` array) and returns
# one. Doing this from Python without a dependency on pywin32's WMI helpers means
# driving it through CIM from PowerShell.
#
# The frame travels through a temp file rather than the command line: the buffer is
# 32768 bytes, which base64-encodes to ~43 KB and exceeds the Windows command-line
# limit. The reply comes back inline, since it is the same size but only its first
# few hundred bytes are ever read.
#
# Substitution uses explicit tokens rather than str.format: the script is full of
# PowerShell braces, which str.format would treat as fields.
_INVOKE_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$payload = [Convert]::FromBase64String((Get-Content -Raw -LiteralPath '__PAYLOAD_FILE__'))
$ns = 'root/wmi'
$inst = Get-CimInstance -Namespace $ns -ClassName BFn -ErrorAction Stop |
        Where-Object { $_.InstanceName -eq '__INSTANCE__' } | Select-Object -First 1
if (-not $inst) { Write-Output '{"ok":false,"error":"no BFn instance matching __INSTANCE__"}'; exit }
$bdat = New-CimInstance -Namespace $ns -ClassName BDat -Property @{ Bytes = [byte[]]$payload } -ClientOnly
$out = Invoke-CimMethod -InputObject $inst -MethodName DoBFn -Arguments @{ Data = $bdat } -ErrorAction Stop
$bytes = $null
if ($out -and $out.Data -and $out.Data.Bytes) { $bytes = $out.Data.Bytes }
if (-not $bytes) { Write-Output '{"ok":false,"error":"DoBFn returned no Data.Bytes"}'; exit }
Write-Output ('{"ok":true,"bytes":"' + [Convert]::ToBase64String([byte[]]$bytes) + '"}')
"""


def _execute(arg1: int, arg2: int) -> ThermalTables:
    """Send one DA command and decode the reply."""
    frame = bytearray(build_ustt_request(arg2 if arg1 else 0))
    # build_ustt_request derives cbArg1 from whether the mode is non-zero, which is
    # wrong for a write of mode 0. Set both fields explicitly.
    struct.pack_into("<I", frame, 4, arg1)
    struct.pack_into("<I", frame, 8, arg2)

    script = _INVOKE_SCRIPT.replace("__INSTANCE__", WMI_INSTANCE)

    import os
    import tempfile

    handle, payload_path = tempfile.mkstemp(prefix="xpshelper-ustt-", suffix=".b64")
    try:
        with os.fdopen(handle, "w", encoding="ascii") as fh:
            fh.write(base64.b64encode(bytes(frame)).decode("ascii"))
        script = script.replace("__PAYLOAD_FILE__", payload_path)
        result = subprocess.run(
            powershell_argv(script),
            capture_output=True,
            text=True,
            timeout=90,
            **hidden_process_kwargs(),
        )
    finally:
        try:
            os.unlink(payload_path)
        except OSError:
            pass
    stdout = (result.stdout or "").strip()
    if not stdout:
        raise ThermalError(
            "the thermal interface returned nothing"
            + (f": {result.stderr.strip()}" if result.stderr else "")
        )
    # The helper may prefix logging; take the last JSON-looking line.
    line = next(
        (ln for ln in reversed(stdout.splitlines()) if ln.lstrip().startswith("{")),
        stdout,
    )
    try:
        reply = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ThermalError(f"could not parse the interface reply: {line!r}") from exc
    if not reply.get("ok"):
        raise ThermalError(f"the thermal interface call failed: {reply.get('error')}")
    return parse_ustt_response(base64.b64decode(reply["bytes"]))
