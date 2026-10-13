"""Native Dell BIOS-attribute control over WMI-ACPI. **No Dell software needed.**

This is the primary control backend on Windows. Dell's BIOS exposes a WMI-ACPI
interface (ACPI device ``PNP0C14``) on machines built from roughly 2018 onward,
which the OS surfaces as:

    namespace  root\\dcim\\sysman\\biosattributes
    read       EnumerationAttribute / IntegerAttribute / StringAttribute / PassobjAttribute
               -> AttributeName, CurrentValue, PossibleValues
    write      BIOSAttributeInterface -> SetAttribute(AttributeName, AttributeValue, ...)

Crucially this comes from the **firmware**, not from an installed agent -- Dell's
own whitepaper for it is titled "Agentless Client Manageability". That means the
thermal profile and battery charge limit can be changed without downloading Dell
Power Manager, Dell Command | Configure, or Dell Command | Monitor.

How this was established
-----------------------
The interface is the same one the Linux kernel drives through ``dell-wmi-sysman``
(WMI GUIDs ``F1DDEE52-063C-4784-A11E-8A06684B9BF4`` for the attribute interface
and ``...9BF5`` / ``...9BFA`` / ``...9BF9`` for the enumeration/integer/string
classes). The Windows-side namespace, class and method names, the security-handle
arguments for a BIOS password, and the status-code table were taken from
Dell-authored PowerShell written against this interface (see CREDITS.md).

Note on the attribute *values*: they are the same BIOS attributes cctk uses, so
the tokens are identical (``UltraPerformance``, ``PrimAcUse``, ``Custom:60-80``).
That is why the token tables in :mod:`xpshelper.model` are shared by both
backends.

Security note
-------------
The BIOS password never appears as a PowerShell string literal. Requests are
JSON-encoded, base64-wrapped, and decoded inside PowerShell, so a password
containing quotes, backticks or ``$`` cannot break out into the command text.
"""

from __future__ import annotations

import base64
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

from .. import constants as C
from ..errors import (
    AttributeNotSupported,
    BackendUnavailableError,
    BiosPasswordInvalid,
    BiosPasswordRequired,
    CctkError,
)
from ..model import (
    Capabilities,
    ChargeConfig,
    ChargeMode,
    ThermalProfile,
    parse_charge_mode_list,
    parse_profile_list,
)

log = logging.getLogger(__name__)

#: Attributes this app cares about, in preference order per feature.
THERMAL_ATTRIBUTE_CANDIDATES = ("ThermalManagement",)
CHARGE_ATTRIBUTE_CANDIDATES = ("PrimaryBattChargeCfg", "PrimaryBatteryChargeCfg")
ADV_CHARGE_ATTRIBUTE = "AdvBatteryChargeCfg"


READ_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'
$ns = 'root\dcim\sysman\biosattributes'
$wanted = @($payload.attributes)

$out = [ordered]@{
  interface_found = $false
  admin_password_set = $null
  attributes = @()
  error = $null
}

try {
  # Presence of this class is what tells us the BIOS exposes WMI-ACPI attributes.
  $null = Get-CimInstance -Namespace $ns -ClassName 'BIOSAttributeInterface' -ErrorAction Stop
  $out.interface_found = $true
} catch {
  $out.error = $_.Exception.Message
}

if ($out.interface_found) {
  foreach ($class in $payload.classes) {
    try {
      $items = Get-CimInstance -Namespace $ns -ClassName $class -ErrorAction Stop |
        Where-Object { $wanted -contains $_.AttributeName } |
        Select-Object AttributeName, CurrentValue, PossibleValues, @{n='AttributeClass';e={$class}}
      if ($items) { $out.attributes += @($items) }
    } catch { }
  }

  try {
    $pw = Get-CimInstance -Namespace 'root\dcim\sysman\wmisecurity' `
            -ClassName 'PasswordObject' -Filter "NameId='Admin'" -ErrorAction Stop
    if ($pw) { $out.admin_password_set = [bool]$pw.IsPasswordSet }
  } catch { }
}

$out | ConvertTo-Json -Compress -Depth 5
"""

WRITE_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$ns = 'root\dcim\sysman\biosattributes'
$out = [ordered]@{ status = $null; error = $null }
try {
  $iface = Get-CimInstance -Namespace $ns -ClassName 'BIOSAttributeInterface' -ErrorAction Stop

  $arguments = @{
    AttributeName  = $payload.attribute
    AttributeValue = $payload.value
  }
  if ($payload.password) {
    $bytes = [Text.Encoding]::UTF8.GetBytes($payload.password)
    $arguments.SecType     = 1
    $arguments.SecHndCount = $bytes.Length
    $arguments.SecHandle   = $bytes
  }

  $result = Invoke-CimMethod -InputObject $iface -MethodName 'SetAttribute' `
              -Arguments $arguments -ErrorAction Stop
  $out.status = [int]$result.Status
} catch {
  $out.error = $_.Exception.Message
}
$out | ConvertTo-Json -Compress
"""

RESULT_MARKER = "@@XPSHELPER@@"

#: Inject the payload safely: JSON -> base64 -> decoded inside PowerShell.
_PAYLOAD_PREAMBLE = (
    "$payload = [Text.Encoding]::UTF8.GetString("
    "[Convert]::FromBase64String('{encoded}')) | ConvertFrom-Json\n"
)

Runner = Callable[[str, int], tuple[int, str, str]]


def default_powershell_runner(script: str, timeout: int) -> tuple[int, str, str]:
    """Run a PowerShell script, returning (returncode, stdout, stderr)."""
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv prefix, no shell
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise BackendUnavailableError(
            "PowerShell was not found on PATH. The native BIOS interface needs it."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CctkError(f"PowerShell timed out after {timeout}s", output=str(exc)) from exc
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def encode_payload(payload: dict[str, Any]) -> str:
    """Base64-encode a JSON payload for safe interpolation into PowerShell."""
    raw = json.dumps(payload).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def build_script(script: str, payload: dict[str, Any]) -> str:
    return _PAYLOAD_PREAMBLE.format(encoded=encode_payload(payload)) + script


# ---------------------------------------------------------------------------
# Pure parsing helpers (unit-tested without hardware)
# ---------------------------------------------------------------------------
def parse_json_output(stdout: str) -> dict[str, Any]:
    """Decode the JSON document our PowerShell scripts emit.

    PowerShell may prefix noise (progress, warnings) and can emit the payload as
    an object or inside an array, so this is deliberately forgiving.
    """
    text = (stdout or "").strip()
    if not text:
        return {}
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return {}
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        log.debug("could not decode JSON from PowerShell output")
        return {}
    if isinstance(data, list):
        return data[0] if data and isinstance(data[0], dict) else {}
    return data if isinstance(data, dict) else {}


def normalise_attribute_list(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def possible_values(entry: dict[str, Any]) -> list[str]:
    """Extract the allowed values for an attribute.

    The property is ``PossibleValues`` on current BIOSes; older/other revisions
    expose a description string instead. Both are handled, and a comma/semicolon
    separated string is split.
    """
    for key in ("PossibleValues", "PossibleValuesDescription", "PossibleValue"):
        raw = entry.get(key)
        if raw is None:
            continue
        if isinstance(raw, list):
            values = [str(item).strip() for item in raw if str(item).strip()]
        else:
            text = str(raw).strip()
            if not text:
                continue
            separator = ";" if ";" in text else ","
            values = [chunk.strip() for chunk in text.split(separator) if chunk.strip()]
        if values:
            return values
    return []


def current_value(entry: dict[str, Any]) -> str | None:
    raw = entry.get("CurrentValue")
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def find_attribute(entries: Sequence[dict[str, Any]], names: Sequence[str]) -> dict[str, Any] | None:
    """Find the first entry whose AttributeName matches any candidate."""
    wanted = {name.lower() for name in names}
    for entry in entries:
        name = str(entry.get("AttributeName") or "").strip().lower()
        if name in wanted:
            return entry
    return None


def status_to_exception(
    status: int | None, *, attribute: str, value: str, output: str = ""
) -> Exception | None:
    """Map a ``SetAttribute`` status code to a typed exception, or None on success.

    This is the same classification the cctk backend performs from exit codes:
    "you need a password" and "your model does not have this setting" are
    completely different user actions and must not collapse into one error.
    """
    if status is None:
        return CctkError(
            f"Setting {attribute}={value} produced no status from the BIOS. {output}".strip(),
            output=output,
        )
    if status == C.WMI_BIOS_STATUS_OK:
        return None
    description = C.WMI_BIOS_STATUS_TEXT.get(status, f"Unknown status {status}")
    if status == C.WMI_BIOS_STATUS_ACCESS_DENIED:
        return BiosPasswordRequired(
            f"Setting {attribute} was denied: the BIOS has an administrator password "
            f"set. Provide it in the app settings or the {C.ENV_BIOS_PASSWORD} "
            "environment variable.",
            exit_code=status,
            output=output,
        )
    if status == C.WMI_BIOS_STATUS_NOT_SUPPORTED:
        return AttributeNotSupported(
            f"This BIOS does not support {attribute}={value}: {description}.",
            exit_code=status,
            output=output,
        )
    if status == C.WMI_BIOS_STATUS_INVALID_PARAMETER:
        return AttributeNotSupported(
            f"The BIOS rejected {attribute}={value} as an invalid value for this "
            f"model: {description}.",
            exit_code=status,
            output=output,
        )
    return CctkError(
        f"Setting {attribute}={value} failed: {description} (status {status}).",
        exit_code=status,
        output=output,
    )


class WmiBiosBackend:
    """Control backend using the firmware's own WMI-ACPI BIOS attributes."""

    name = "wmi-bios"

    def __init__(
        self,
        *,
        bios_password: str | None = None,
        runner: Runner | None = None,
        timeout: int = 45,
    ) -> None:
        import os

        self._runner = runner or default_powershell_runner
        self.timeout = timeout
        self.bios_password = bios_password or os.environ.get(C.ENV_BIOS_PASSWORD) or None
        self._cache: dict[str, Any] | None = None
        self.last_error: str | None = None

    # -- availability -----------------------------------------------------
    @property
    def available(self) -> bool:
        return sys.platform == "win32"

    def is_elevated(self) -> bool:
        if sys.platform != "win32":
            return False
        try:
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return False

    # -- low level --------------------------------------------------------
    def _run(self, script: str, payload: dict[str, Any]) -> dict[str, Any]:
        _code, stdout, stderr = self._runner(build_script(script, payload), self.timeout)
        data = parse_json_output(stdout)
        if not data and stderr.strip():
            self.last_error = stderr.strip()[:400]
        return data

    def refresh(self) -> dict[str, Any]:
        """Query the BIOS for the attributes we care about."""
        if not self.available:
            self.last_error = "The native BIOS interface is only available on Windows."
            self._cache = {}
            return self._cache

        wanted = list(THERMAL_ATTRIBUTE_CANDIDATES) + list(CHARGE_ATTRIBUTE_CANDIDATES)
        wanted.append(ADV_CHARGE_ATTRIBUTE)
        data = self._run(
            READ_SCRIPT,
            {"attributes": wanted, "classes": list(C.WMI_BIOS_ATTRIBUTE_CLASSES)},
        )
        self._cache = data
        if not data.get("interface_found"):
            self.last_error = data.get("error") or (
                "The BIOS did not expose the WMI-ACPI BIOS attribute interface. "
                "This machine may not support it."
            )
        return data

    def _snapshot(self, *, use_cache: bool = True) -> dict[str, Any]:
        if self._cache is None or not use_cache:
            return self.refresh()
        return self._cache

    def _entries(self, *, use_cache: bool = True) -> list[dict[str, Any]]:
        return normalise_attribute_list(self._snapshot(use_cache=use_cache).get("attributes"))

    # -- capabilities -----------------------------------------------------
    def probe(self, *, use_cache: bool = True) -> Capabilities:
        caps = Capabilities(control_backend=self.name)
        data = self._snapshot(use_cache=use_cache)

        if not self.available:
            caps.notes.append("Native BIOS interface is only available on Windows.")
            return caps
        if not data.get("interface_found"):
            caps.notes.append(
                "The BIOS did not expose BIOSAttributeInterface. Either this model "
                "predates WMI-ACPI support, or the interface is disabled. Run "
                "'xps-helper doctor' for details."
            )
            if data.get("error"):
                caps.notes.append(f"BIOS said: {data['error']}")
            return caps

        entries = self._entries(use_cache=use_cache)
        found_names = sorted(
            {str(e.get("AttributeName")) for e in entries if e.get("AttributeName")}
        )
        caps.notes.append(
            f"Native BIOS attributes available: {', '.join(found_names) or 'none of the expected ones'}"
        )
        if data.get("admin_password_set"):
            caps.notes.append(
                "A BIOS administrator password is set; writes need it."
            )

        thermal = find_attribute(entries, THERMAL_ATTRIBUTE_CANDIDATES)
        if thermal is not None:
            values = possible_values(thermal)
            caps.thermal_profiles = parse_profile_list(values) if values else tuple(ThermalProfile)
            if not values:
                caps.notes.append(
                    "ThermalManagement reports no PossibleValues; showing all documented modes."
                )

        charge = find_attribute(entries, CHARGE_ATTRIBUTE_CANDIDATES)
        if charge is not None:
            values = possible_values(charge)
            caps.charge_modes = parse_charge_mode_list(values) if values else tuple(ChargeMode)
            if not values:
                caps.notes.append(
                    "The charge attribute reports no PossibleValues; showing all documented modes."
                )

        adv = find_attribute(entries, (ADV_CHARGE_ATTRIBUTE,))
        caps.supports_advanced_schedule = adv is not None
        return caps

    # -- reads ------------------------------------------------------------
    def read_profile(self) -> ThermalProfile | None:
        entry = find_attribute(self._entries(), THERMAL_ATTRIBUTE_CANDIDATES)
        if entry is None:
            return None
        return ThermalProfile.from_token(current_value(entry) or "")

    def read_charge_config(self) -> ChargeConfig | None:
        entry = find_attribute(self._entries(), CHARGE_ATTRIBUTE_CANDIDATES)
        if entry is None:
            return None
        return ChargeConfig.from_token(current_value(entry) or "")

    def password_is_set(self) -> bool | None:
        return self._snapshot().get("admin_password_set")

    # -- writes -----------------------------------------------------------
    def _set_attribute(self, attribute: str, value: str) -> None:
        data = self._run(
            WRITE_SCRIPT,
            {"attribute": attribute, "value": value, "password": self.bios_password},
        )
        status = data.get("status")
        if status is not None:
            try:
                status = int(status)
            except (TypeError, ValueError):
                status = None

        problem = status_to_exception(
            status, attribute=attribute, value=value, output=str(data.get("error") or "")
        )
        if problem is not None:
            raise problem

    def set_profile(self, profile: ThermalProfile) -> None:
        self._set_attribute(C.ATTR_THERMAL, profile.token)
        self._cache = None  # force a re-read so verification sees the truth

    def set_charge_config(self, config: ChargeConfig) -> None:
        entry = find_attribute(self._entries(), CHARGE_ATTRIBUTE_CANDIDATES)
        attribute = (
            str(entry.get("AttributeName")) if entry else C.ATTR_PRIMARY_BATT
        )
        self._set_attribute(attribute, config.token)
        self._cache = None
