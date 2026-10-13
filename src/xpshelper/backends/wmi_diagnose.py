"""Deep WMI diagnostics for Dell BIOS interfaces (`xps-helper diagnose-wmi`).

Exists because "the BIOS did not expose BIOSAttributeInterface" is not a useful
thing to tell someone: it could mean the model lacks WMI-ACPI entirely, that the
namespace is named differently, that the class needs the legacy DCOM provider
rather than CIM, or that the query was simply denied without elevation. Those
need different fixes, and guessing between them from a single boolean wastes
everybody's time.

This runs every candidate probe **twice** (CIM and DCOM), records the exact
exception text from each, enumerates the namespaces and classes that do exist,
and lists the ACPI-WMI (``PNP0C14``) devices the firmware actually provides. The
output is designed to be pasted into a bug report as-is.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from .. import constants as C
from ..platform import bios_version, is_dell, system_model, system_vendor
from ..procutil import hidden_process_kwargs, powershell_argv
from .wmi_bios import parse_json_output

log = logging.getLogger(__name__)

DIAGNOSE_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'
$out = [ordered]@{}

$out.powershell = $PSVersionTable.PSVersion.ToString()
try {
  $id = [Security.Principal.WindowsIdentity]::GetCurrent()
  $out.elevated = ([Security.Principal.WindowsPrincipal]$id).IsInRole(
      [Security.Principal.WindowsBuiltInRole]::Administrator)
} catch { $out.elevated = $null }

# --- ACPI-WMI devices: PNP0C14 is the device Windows creates for every
# --- firmware-provided WMI mapping. If there are none, no WMI-ACPI interface
# --- exists on this machine at all.
$out.acpi_wmi_devices = @()
try {
  $out.acpi_wmi_devices = @(
    Get-CimInstance -ClassName Win32_PnPEntity -ErrorAction Stop |
      Where-Object { $_.DeviceID -match '^ACPI\\PNP0C14' } |
      ForEach-Object { "$($_.DeviceID) $(if ($_.Name) { $_.Name })" }
  )
} catch { }

# --- Which namespaces exist?
$namespaces = @()
foreach ($parent in @('root', 'root\dcim', 'root\dcim\sysman', 'root\wmi')) {
  try {
    $namespaces += @(
      Get-CimInstance -Namespace $parent -ClassName '__NAMESPACE' -ErrorAction Stop |
        ForEach-Object { "$parent\$($_.Name)" }
    )
  } catch { }
}
$out.namespaces = $namespaces

# --- Probe every candidate class with BOTH providers.
# --- Some Dell ACPI-WMI classes are not reachable over WSMan/CIM; Dell's own
# --- sample scripts use Get-WmiObject for root\dcim\sysman\biosattributes.
$probes = @()
$targets = @(
  @{ ns = 'root\dcim\sysman\biosattributes'; classes = @('BIOSAttributeInterface','EnumerationAttribute','IntegerAttribute','StringAttribute','PassobjAttribute') },
  @{ ns = 'root\dcim\sysman';                classes = @('DCIM_BIOSService','DCIM_BIOSEnumeration','DCIM_BIOSAttribute') },
  @{ ns = 'root\dcim\sysman\wmisecurity';    classes = @('PasswordObject','SecurityInterface') }
)
foreach ($target in $targets) {
  foreach ($class in $target.classes) {
    $record = [ordered]@{
      namespace = $target.ns
      class     = $class
      cim       = $null
      cim_error = $null
      wmi       = $null
      wmi_error = $null
      count     = $null
      attributes_sampled = @()
    }
    try {
      $items = @(Get-CimInstance -Namespace $target.ns -ClassName $class -ErrorAction Stop)
      $record.cim = 'ok'
      $record.count = $items.Count
      $record.attributes_sampled = @($items | Select-Object -First 5 | ForEach-Object { $_.AttributeName })
    } catch { $record.cim_error = $_.Exception.Message }

    try {
      $items = @(Get-WmiObject -Namespace $target.ns -Class $class -ErrorAction Stop)
      $record.wmi = 'ok'
      if ($null -eq $record.count) { $record.count = $items.Count }
      if (-not $record.attributes_sampled) {
        $record.attributes_sampled = @($items | Select-Object -First 5 | ForEach-Object { $_.AttributeName })
      }
    } catch { $record.wmi_error = $_.Exception.Message }

    $probes += $record
  }
}
$out.probes = $probes

# --- Any root\wmi classes whose names hint at a Dell interface.
$out.wmi_classes_of_interest = @()
try {
  $out.wmi_classes_of_interest = @(
    Get-CimInstance -Namespace 'root\wmi' -ClassName '__CLASS' -ErrorAction Stop |
      Where-Object { $_.Name -match 'Dell|BIOS|Bios|Attr|Enum|Thermal|SysMan|Smm' } |
      ForEach-Object { $_.Name }
  )
} catch { }

# --- The two attribute names we care about, if anything exposes them.
$out.target_attributes = @()
foreach ($ns in @('root\dcim\sysman\biosattributes')) {
  foreach ($class in @('EnumerationAttribute','IntegerAttribute','StringAttribute')) {
    foreach ($provider in @('cim','wmi')) {
      try {
        if ($provider -eq 'cim') {
          $items = Get-CimInstance -Namespace $ns -ClassName $class -ErrorAction Stop
        } else {
          $items = Get-WmiObject -Namespace $ns -Class $class -ErrorAction Stop
        }
      } catch { continue }
      foreach ($item in $items) {
        if ($item.AttributeName -in @('ThermalManagement','PrimaryBattChargeCfg','AdvBatteryChargeCfg')) {
          $out.target_attributes += [ordered]@{
            provider       = $provider
            class          = $class
            AttributeName  = $item.AttributeName
            CurrentValue   = "$($item.CurrentValue)"
            PossibleValues = "$($item.PossibleValues)"
          }
        }
      }
    }
  }
}

$out | ConvertTo-Json -Compress -Depth 6
"""


def run_diagnosis(*, runner: Callable[[list[str], int], tuple[int, str, str]] | None = None, timeout: int = 120) -> dict[str, Any]:
    """Run the diagnostic script and return the decoded report."""
    import subprocess
    import sys

    if sys.platform != "win32" and runner is None:
        return {
            "error": "WMI diagnosis is only meaningful on Windows.",
            "platform": sys.platform,
        }

    if runner is None:

        def runner(argv: list[str], timeout: int):  # type: ignore[misc]
            proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                errors="replace",
                **hidden_process_kwargs(),
            )
            return proc.returncode, proc.stdout or "", proc.stderr or ""

    _code, stdout, stderr = runner(powershell_argv(DIAGNOSE_SCRIPT), timeout)
    data = parse_json_output(stdout)
    if not data:
        data = {"error": "Could not decode the diagnostic output.", "stderr": stderr.strip()[:2000]}
    data.setdefault("system", {
        "vendor": system_vendor(),
        "model": system_model(),
        "bios": bios_version(),
        "is_dell": is_dell(),
    })
    return data


def _yes_no(value: Any) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "unknown"


def render(diagnosis: dict[str, Any]) -> str:
    """Render the diagnosis as paste-ready text."""
    lines: list[str] = []
    lines.append(f"{C.APP_DISPLAY_NAME} WMI diagnosis")
    lines.append("=" * 70)

    system = diagnosis.get("system") or {}
    lines.append(
        f"Machine     : {system.get('vendor')} {system.get('model')} "
        f"(Dell: {_yes_no(system.get('is_dell'))})"
    )
    lines.append(f"BIOS        : {system.get('bios')}")
    lines.append(f"PowerShell  : {diagnosis.get('powershell')}")
    lines.append(f"Elevated    : {_yes_no(diagnosis.get('elevated'))}")

    if diagnosis.get("error"):
        lines.append("")
        lines.append(f"ERROR: {diagnosis['error']}")
        if diagnosis.get("stderr"):
            lines.append(diagnosis["stderr"])

    devices = diagnosis.get("acpi_wmi_devices") or []
    lines.append("")
    lines.append(f"ACPI-WMI devices (ACPI\\PNP0C14) : {len(devices)}")
    for device in devices[:20]:
        lines.append(f"    {device}")
    if not devices:
        lines.append("    NONE. Without an ACPI-WMI device the firmware exposes no")
        lines.append("    WMI management interface, so this path cannot work on this")
        lines.append("    machine regardless of provider.")

    lines.append("")
    lines.append("Candidate interface probes")
    lines.append("-" * 70)
    for probe in diagnosis.get("probes") or []:
        label = f"{probe.get('namespace')}\\{probe.get('class')}"
        cim = probe.get("cim")
        wmi = probe.get("wmi")
        lines.append(f"{label}")
        lines.append(f"    CIM  : {'OK' if cim else 'unavailable'}"
                     + (f" ({probe.get('count')} objects)" if cim else ""))
        if not cim and probe.get("cim_error"):
            lines.append(f"           {probe['cim_error']}")
        lines.append(f"    WMI  : {'OK' if wmi else 'unavailable'}"
                     + (f" ({probe.get('count')} objects)" if wmi else ""))
        if not wmi and probe.get("wmi_error"):
            lines.append(f"           {probe['wmi_error']}")
        sample = probe.get("attributes_sampled") or []
        if sample:
            lines.append(f"    sample: {', '.join(str(s) for s in sample if s)}")

    targets = diagnosis.get("target_attributes") or []
    lines.append("")
    lines.append("Target attributes found")
    lines.append("-" * 70)
    if targets:
        for item in targets:
            lines.append(
                f"    [{item.get('provider')}/{item.get('class')}] "
                f"{item.get('AttributeName')} = {item.get('CurrentValue')}"
            )
            lines.append(f"        PossibleValues: {item.get('PossibleValues')}")
    else:
        lines.append("    NONE of ThermalManagement / PrimaryBattChargeCfg were found.")

    namespaces = diagnosis.get("namespaces") or []
    lines.append("")
    lines.append(f"Namespaces present ({len(namespaces)})")
    lines.append("-" * 70)
    for namespace in namespaces[:60]:
        lines.append(f"    {namespace}")

    interesting = diagnosis.get("wmi_classes_of_interest") or []
    lines.append("")
    lines.append(f"root\\wmi classes that look Dell-related ({len(interesting)})")
    lines.append("-" * 70)
    for name in interesting[:40]:
        lines.append(f"    {name}")

    lines.append("")
    lines.append("=" * 70)
    lines.append("Paste everything above when reporting a problem.")
    return "\n".join(lines)


def to_json(diagnosis: dict[str, Any]) -> str:
    return json.dumps(diagnosis, indent=2, default=str)
