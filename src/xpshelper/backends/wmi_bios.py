"""Native Dell BIOS-attribute control over WMI-ACPI. **No Dell software needed.**

This is the primary control backend on Windows. Dell's BIOS exposes a WMI-ACPI
interface (ACPI device ``PNP0C14``) on machines built from roughly 2018 onward,
which the OS surfaces as:

    namespace  root\\dcim\\sysman\\biosattributes
    read       EnumerationAttribute / IntegerAttribute / StringAttribute / PassobjAttribute
               -> AttributeName, CurrentValue, PossibleValues
    write      BIOSAttributeInterface -> SetAttribute(AttributeName, AttributeValue, ...)

Crucially this comes from the **firmware**, not from an installed agent -- Dell's
own whitepaper for it is titled "Agentless Client Manageability".

Two providers, and why both are tried
-------------------------------------
WMI exposes the same class through two different stacks: the modern CIM/WSMan
provider (``Get-CimInstance``) and the legacy DCOM provider (``Get-WmiObject``).
They are not interchangeable: **some firmware ACPI-WMI classes are reachable only
over one of them.** Dell's own sample scripts query this exact namespace with
``Get-WmiObject``, while community forks use ``Get-CimInstance`` -- evidence that
which one works depends on the machine. Querying only CIM therefore produced a
false "your BIOS does not have this interface" on machines where it does, so
every read and write here tries CIM first and falls back to DCOM, recording the
error from each.

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
import re
import subprocess
import sys
from typing import Any, Callable, Sequence

from .. import constants as C
from ..errors import (
    AttributeNotSupported,
    BackendUnavailableError,
    BiosPasswordRequired,
    CctkError,
    ValidationError,
)
from ..model import (
    Capabilities,
    ChargeConfig,
    ChargeMode,
    ThermalProfile,
    parse_charge_mode_list,
    parse_profile_list,
)
from ..procutil import hidden_process_kwargs, powershell_argv

log = logging.getLogger(__name__)

#: Attributes this app cares about, per feature.
THERMAL_ATTRIBUTE_CANDIDATES = ("ThermalManagement",)
CHARGE_ATTRIBUTE_CANDIDATES = ("PrimaryBattChargeCfg", "PrimaryBatteryChargeCfg")
ADV_CHARGE_ATTRIBUTE = "AdvBatteryChargeCfg"

#: Custom ("charge between N% and M%") mode is *not* expressed as a single
#: ``Custom:60-80`` token over WMI-ACPI. Verified against an XPS 13 7390 2-in-1
#: (BIOS 1.35.0): writing ``PrimaryBattChargeCfg=Custom:60-80`` returns status 1
#: (Failed), while writing ``PrimaryBattChargeCfg=Custom`` and then the two
#: integer attributes below returns status 0 each. The combined ``Custom:60-80``
#: spelling is the cctk command-line syntax, not the WMI one -- sending it here
#: simply does nothing, which is why custom thresholds must be handled as three
#: separate attribute writes.
CUSTOM_START_ATTRIBUTE = "CustomChargeStart"
CUSTOM_STOP_ATTRIBUTE = "CustomChargeStop"

#: Whether the interface needs a security handle even when no BIOS password is
#: set. On this hardware ``SetAttribute`` rejects a call that omits SecHandle /
#: SecHndCount with "Invalid parameter", but accepts ``SecType=0`` (NONE) with a
#: zero-length SecHandle. See :data:`WMI_SEC_TYPE_NONE`.
WMI_SEC_TYPE_NONE = 0
WMI_SEC_TYPE_PLAINTEXT = 1

#: Recognises a permissions refusal in a WMI error message. The wording differs
#: by provider -- CIM says "Access denied", the legacy provider says "Access is
#: denied" -- and both must be caught, because treating this as "the feature does
#: not exist" points the user at completely the wrong fix.
WMI_ACCESS_DENIED_RE = r"access\s+(?:is\s+)?denied|privilege|elevat"

#: Attributes read for their *value* rather than to build a capability list.
VALUE_ATTRIBUTE_CANDIDATES = (CUSTOM_START_ATTRIBUTE, CUSTOM_STOP_ATTRIBUTE)


READ_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'
$ns = 'root\dcim\sysman\biosattributes'
$wanted = @($payload.attributes)
# Attributes read for their values only (used by custom charge thresholds).
$wantValues = @($payload.value_attributes)

$out = [ordered]@{
  interface_found    = $false
  namespace_exists   = $false
  access_denied      = $false
  api                = $null
  elevated           = $false
  admin_password_set = $null
  attributes         = @()
  errors             = @()
}

try {
  $out.elevated = [bool]([Security.Principal.WindowsPrincipal] `
      [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
      [Security.Principal.WindowsBuiltInRole]::Administrator)
} catch { }

# Does the namespace exist at all? Enumerating __NAMESPACE of the *parent* works
# without elevation, whereas querying inside it may not. This is what separates
# "your firmware has no such interface" from "you need to run this as
# administrator" -- two completely different user actions.
try {
  $children = Get-CimInstance -Namespace 'root\dcim\sysman' -ClassName '__NAMESPACE' -ErrorAction Stop
  if (@($children | Where-Object { $_.Name -eq 'biosattributes' }).Count -gt 0) {
    $out.namespace_exists = $true
  }
} catch { }

# The interface class. CIM first, then the legacy DCOM provider: some Dell
# ACPI-WMI classes are not reachable over WSMan/CIM and Dell's own scripts use
# Get-WmiObject for this namespace.
$iface = $null
foreach ($api in @('cim', 'wmi')) {
  try {
    if ($api -eq 'cim') {
      $iface = Get-CimInstance -Namespace $ns -ClassName 'BIOSAttributeInterface' -ErrorAction Stop
    } else {
      $iface = Get-WmiObject -Namespace $ns -Class 'BIOSAttributeInterface' -ErrorAction Stop
    }
    $out.api = $api
    break
  } catch {
    $msg = "$($_.Exception.Message)"
    $out.errors += "${api}:BIOSAttributeInterface: $msg"
    if ($msg -match 'access\s+(is\s+)?denied|privilege|elevat') { $out.access_denied = $true }
  }
}
if ($null -ne $iface) { $out.interface_found = $true }

if ($out.interface_found) {
  foreach ($class in $payload.classes) {
    $items = $null
    foreach ($api in @('cim', 'wmi')) {
      try {
        if ($api -eq 'cim') {
          $items = Get-CimInstance -Namespace $ns -ClassName $class -ErrorAction Stop
        } else {
          $items = Get-WmiObject -Namespace $ns -Class $class -ErrorAction Stop
        }
      } catch {
        $out.errors += "${api}:${class}: $($_.Exception.Message)"
        continue
      }
      if (@($items).Count -gt 0) { break }
    }
    if ($null -eq $items) { continue }

    # Project only the properties that exist on this class. EnumerationAttribute
    # carries PossibleValue/PossibleValueCount/ReadOnly/DisplayName;
    # IntegerAttribute has bounds and no possible-value list. Build the row
    # explicitly rather than using Select-Object so a missing property cannot
    # silently drop the whole attribute.
    foreach ($item in $items) {
      $name = "$($item.AttributeName)"
      if ([string]::IsNullOrWhiteSpace($name)) { continue }
      $interesting = ($wanted -contains $name) -or ($wantValues -contains $name)

      $pv = @()
      $pvc = $null
      if ($null -ne $item.PSObject.Properties['PossibleValue']) {
        if ($null -ne $item.PossibleValue) { $pv = @($item.PossibleValue) }
      }
      if ($null -ne $item.PSObject.Properties['PossibleValueCount']) {
        $pvc = $item.PossibleValueCount
      }
      # Older revisions named it in the plural; keep that working.
      if ($pv.Count -eq 0 -and $null -ne $item.PSObject.Properties['PossibleValues']) {
        if ($null -ne $item.PossibleValues) { $pv = @($item.PossibleValues) }
      }

      $row = [ordered]@{
        AttributeName      = $name
        CurrentValue       = if ($null -ne $item.CurrentValue) { "$($item.CurrentValue)" } else { $null }
        PossibleValue      = $pv
        PossibleValueCount = $pvc
        AttributeClass     = $class
        DisplayName        = if ($null -ne $item.PSObject.Properties['DisplayName']) { "$($item.DisplayName)" } else { $null }
        ReadOnly           = if ($null -ne $item.PSObject.Properties['ReadOnly']) { [bool]$item.ReadOnly } else { $null }
        LowerBound         = if ($null -ne $item.PSObject.Properties['LowerBound']) { $item.LowerBound } else { $null }
        UpperBound         = if ($null -ne $item.PSObject.Properties['UpperBound']) { $item.UpperBound } else { $null }
      }
      if ($interesting) { $out.attributes += $row }
    }
  }

  try {
    $pw = Get-CimInstance -Namespace 'root\dcim\sysman\wmisecurity' `
            -ClassName 'PasswordObject' -Filter "NameId='Admin'" -ErrorAction Stop
    if ($pw) { $out.admin_password_set = [bool]$pw.IsPasswordSet }
  } catch {
    $out.errors += "wmisecurity: $($_.Exception.Message)"
  }
}

$out | ConvertTo-Json -Compress -Depth 5
"""

WRITE_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$ns = 'root\dcim\sysman\biosattributes'
$out = [ordered]@{ api = $null; error = $null; results = @() }

# Writes go through CIM only.
#
# The legacy DCOM provider cannot express this call correctly: SetAttribute's
# SecHandle parameter is declared *before* the outgoing Status, and
# Invoke-WmiMethod's positional binding cannot build a UInt8Array for it from
# PowerShell (it fails with "Unable to cast object of type 'System.Int32' to
# type 'System.Array'"). CIM handles the same method cleanly and was verified to
# return Status=0 on real hardware, so there is one code path here instead of
# two half-working ones.
$iface = $null
try {
  $iface = Get-CimInstance -Namespace $ns -ClassName 'BIOSAttributeInterface' -ErrorAction Stop
  $out.api = 'cim'
} catch {
  $out.error = "cim: $($_.Exception.Message)"
}

if ($null -ne $iface) {
  foreach ($item in $payload.writes) {
    $entry = [ordered]@{ attribute = $item.attribute; status = $null; note = $null; error = $null }
    try {
      # SecHandle is mandatory even with no password: omitting it makes the
      # firmware answer "Invalid parameter", so it is always sent explicitly,
      # zero-length when there is no password to supply.
      $arguments = @{
        AttributeName = $item.attribute
        AttributeValue = $item.value
        SecType = 0
        SecHndCount = 0
        SecHandle = [byte[]]@()
      }
      if ($payload.password) {
        $bytes = [Text.Encoding]::UTF8.GetBytes($payload.password)
        $arguments.SecType = 1
        $arguments.SecHndCount = $bytes.Length
        $arguments.SecHandle = $bytes
      }
      $result = Invoke-CimMethod -InputObject $iface -MethodName 'SetAttribute' `
                  -Arguments $arguments -ErrorAction Stop
      $entry.status = [int]$result.Status
    } catch {
      $msg = "$($_.Exception.Message)"
      # A transport-level failure is not a BIOS status. Record it verbatim so the
      # caller can tell "the firmware said no" from "we never reached the
      # firmware".
      if ($msg -match 'access\s+(is\s+)?denied|privilege|elevat') {
        $entry.status = 3
      }
      $entry.error = $msg
    }
    $out.results += $entry
  }
}

$out | ConvertTo-Json -Compress -Depth 5
"""

#: Inject the payload safely: JSON -> base64 -> decoded inside PowerShell.
_PAYLOAD_PREAMBLE = (
    "$payload = [Text.Encoding]::UTF8.GetString("
    "[Convert]::FromBase64String('{encoded}')) | ConvertFrom-Json\n"
)

Runner = Callable[[str, int], tuple[int, str, str]]


def default_powershell_runner(script: str, timeout: int) -> tuple[int, str, str]:
    """Run a PowerShell script with no console window, returning (rc, out, err).

    ``hidden_process_kwargs`` matters here: the GUI is built without a console, so
    without it Windows flashes a black console window on every sensor refresh.
    """
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            powershell_argv(script),
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",
            **hidden_process_kwargs(),
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


def _as_error_list(value: Any) -> list[str]:
    """Normalise a PowerShell string-or-array field into a list of strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]


def possible_values(
    entry: dict[str, Any], entries: Sequence[dict[str, Any]] | None = None
) -> list[str]:
    """Extract the allowed values for an attribute.

    The property is ``PossibleValue`` (singular, a string array) on the Dell
    WMI-ACPI classes -- confirmed by reading the class schema on an XPS 13 7390
    2-in-1 with BIOS 1.35.0. Earlier revisions of this app looked for
    ``PossibleValues``, found nothing, and fell back to "show every documented
    mode", which silently hid the firmware's real capability list.

    The plural spelling, a comma/semicolon separated description string, and
    integer bounds are all still accepted, and an integer attribute falls back to
    ``LowerBound``/``UpperBound`` so a caller always learns the real range
    instead of a guess.
    """
    for key in ("PossibleValue", "PossibleValues", "PossibleValuesDescription"):
        if key not in entry:
            continue
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

    # Integer attributes have no possible-value list, only bounds. Reporting the
    # range is strictly more useful than reporting nothing.
    lower, upper = entry.get("LowerBound"), entry.get("UpperBound")
    if lower is not None and upper is not None and str(lower) != str(upper):
        try:
            return [str(value) for value in range(int(lower), int(upper) + 1)]
        except (TypeError, ValueError):
            return []

    # Some BIOS revisions expose the advertised values on a sibling attribute.
    if entries:
        name = str(entry.get("AttributeName") or "").strip()
        if name:
            for suffix in ("PossibleValues", f"{name}PossibleValues"):
                sibling = find_attribute(entries, (suffix,))
                if sibling is not None and sibling is not entry:
                    values = possible_values(sibling)
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
        self._value_cache: dict[str, str | None] = {}
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
            {
                "attributes": wanted,
                "value_attributes": list(VALUE_ATTRIBUTE_CANDIDATES),
                "classes": list(C.WMI_BIOS_ATTRIBUTE_CLASSES),
            },
        )
        self._cache = data
        self._value_cache = {}
        if not data.get("interface_found"):
            # PowerShell emits a bare string instead of a one-element array, so
            # normalise before joining.
            errors = _as_error_list(data.get("errors"))
            self.last_error = " | ".join(errors[:3]) or (
                "The BIOS did not expose the WMI-ACPI BIOS attribute interface."
            )
        else:
            self.last_error = None
        return data

    # -- interpreting a refresh -------------------------------------------
    def access_denied(self) -> bool:
        """True when the interface exists but the OS refused us access to it.

        This is the difference between "your model does not have this feature"
        and "run this as administrator". Reporting the first when it is really
        the second sends people looking for a BIOS setting that is not the
        problem, so the two are distinguished explicitly.
        """
        data = self._snapshot()
        if data.get("interface_found"):
            return False
        if data.get("access_denied"):
            return True
        blob = " ".join(_as_error_list(data.get("errors"))).lower()
        return bool(re.search(WMI_ACCESS_DENIED_RE, blob))

    def namespace_present(self) -> bool | None:
        """Whether the BIOS attribute namespace exists, independent of access."""
        return self._snapshot().get("namespace_exists")

    def active_provider(self) -> str | None:
        """'cim' or 'wmi' -- which provider the firmware answered on."""
        return self._snapshot().get("api")

    def _unavailable_reason(self) -> str:
        """A human explanation of why the interface is not usable."""
        if self.access_denied():
            return (
                "The BIOS attribute interface exists on this machine, but Windows "
                "denied access to it. This needs administrator rights: re-run the "
                "app elevated (the GUI offers to do this for you). This is a "
                "permissions problem, not a missing feature."
            )
        if self.namespace_present():
            return (
                f"{C.WMI_BIOS_NAMESPACE} exists but "
                f"{C.WMI_BIOS_INTERFACE_CLASS} could not be read from it. The "
                "interface may be disabled in BIOS setup."
            )
        return (
            f"The BIOS did not expose {C.WMI_BIOS_INTERFACE_CLASS} in "
            f"{C.WMI_BIOS_NAMESPACE}, and that namespace does not exist on this "
            "machine. Either this model predates WMI-ACPI support, or the feature "
            "is disabled in BIOS setup."
        )

    def _read_value(self, attribute: str) -> str | None:
        """Read one attribute's current value.

        Prefers the attributes already in the snapshot. Enumerating
        ``EnumerationAttribute`` costs several seconds on this firmware -- it
        returns all 92 instances and ``-Filter`` does not avoid the walk -- so a
        second PowerShell launch just to re-read a value we already hold is a
        visible waste. The dedicated read is only a fallback for callers that ask
        about something the snapshot did not include.
        """
        if attribute in self._value_cache:
            return self._value_cache[attribute]

        entry = find_attribute(self._entries(), (attribute,))
        if entry is not None:
            value = current_value(entry)
            self._value_cache[attribute] = value
            return value

        data = self._run(
            READ_SCRIPT,
            {
                "attributes": [],
                "value_attributes": [attribute],
                "classes": list(C.WMI_BIOS_ATTRIBUTE_CLASSES),
            },
        )
        entry = find_attribute(normalise_attribute_list(data.get("attributes")), (attribute,))
        value = current_value(entry) if entry is not None else None
        self._value_cache[attribute] = value
        return value

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
            caps.supports_thermal = False
            caps.supports_charge = False
            return caps

        if not data.get("interface_found"):
            caps.notes.append(self._unavailable_reason())
            for error in _as_error_list(data.get("errors"))[:4]:
                caps.notes.append(f"BIOS said: {error}")
            if not data.get("errors") and not self.namespace_present():
                caps.notes.append(
                    "No error was returned either, which usually means the namespace "
                    "does not exist on this model."
                )
            # Nothing here is readable, so no setting can be changed right now.
            # Reporting these as unsupported is what stops the UI offering
            # controls that would only fail: when the cause is missing elevation
            # the elevation prompt is the fix, and when it is a missing interface
            # these really are unsupported. Leaving them "unknown" would fail open
            # and present working-looking buttons that always error.
            caps.supports_thermal = False
            caps.supports_charge = False
            return caps

        api = data.get("api") or "unknown"
        entries = self._entries(use_cache=use_cache)
        found_names = sorted(
            {str(e.get("AttributeName")) for e in entries if e.get("AttributeName")}
        )
        caps.notes.append(
            f"Native BIOS attributes available via the {api.upper()} provider: "
            f"{', '.join(found_names) or 'none of the expected ones'}"
        )
        if data.get("admin_password_set"):
            caps.notes.append("A BIOS administrator password is set; writes need it.")

        thermal = find_attribute(entries, THERMAL_ATTRIBUTE_CANDIDATES)
        if thermal is not None:
            caps.supports_thermal = True
            values = possible_values(thermal, entries)
            caps.thermal_profiles = parse_profile_list(values) if values else tuple(ThermalProfile)
            if not values:
                caps.notes.append(
                    "ThermalManagement reports no PossibleValues; showing all documented modes."
                )
        else:
            # The attribute is genuinely absent, so this feature cannot work here.
            # Recording that explicitly is what lets the GUI disable the controls
            # instead of offering a profile the firmware will reject.
            caps.supports_thermal = False
            caps.notes.append(
                f"This BIOS does not list {THERMAL_ATTRIBUTE_CANDIDATES[0]} among its "
                "attributes, so thermal profiles cannot be changed on this model. "
                "Charge control is unaffected."
            )

        charge = find_attribute(entries, CHARGE_ATTRIBUTE_CANDIDATES)
        if charge is not None:
            caps.supports_charge = True
            values = possible_values(charge, entries)
            caps.charge_modes = parse_charge_mode_list(values) if values else tuple(ChargeMode)
            if not values:
                caps.notes.append(
                    "The charge attribute reports no PossibleValues; showing all documented modes."
                )
        else:
            caps.supports_charge = False
            caps.notes.append(
                f"This BIOS does not list {CHARGE_ATTRIBUTE_CANDIDATES[0]}, so the battery "
                "charge configuration cannot be changed on this model."
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
        raw = current_value(entry) or ""

        # A bare ``Custom`` token carries no thresholds -- they live on two
        # separate integer attributes -- so ChargeConfig.from_token returns None
        # for it by design (a Custom config is invalid without thresholds). Parse
        # the mode first and combine it with the thresholds that the firmware
        # actually stores alongside it.
        mode = ChargeMode.from_token(raw)
        if mode is ChargeMode.CUSTOM:
            start = self._read_value(CUSTOM_START_ATTRIBUTE)
            stop = self._read_value(CUSTOM_STOP_ATTRIBUTE)
            try:
                return ChargeConfig(
                    mode=ChargeMode.CUSTOM,
                    start=int(start) if start is not None else None,
                    stop=int(stop) if stop is not None else None,
                )
            except (TypeError, ValueError, ValidationError):
                # The firmware holds a Custom mode whose thresholds we cannot read
                # back. Reporting nothing is honest; inventing numbers is not.
                log.debug("could not read back the custom charge thresholds")
                return None

        # A combined token (``Custom:60-80``) is what cctk prints, and some BIOS
        # revisions may still report one; from_token handles both shapes.
        return ChargeConfig.from_token(raw)

    def password_is_set(self) -> bool | None:
        return self._snapshot().get("admin_password_set")

    # -- writes -----------------------------------------------------------
    def _write_attributes(self, writes: Sequence[tuple[str, str]]) -> None:
        """Send a batch of attribute writes and raise on the first failure.

        The firmware applies each attribute independently and reports a status
        per attribute, so the batch is validated entry by entry rather than
        trusting a single overall result.
        """
        data = self._run(
            WRITE_SCRIPT,
            {
                "writes": [{"attribute": name, "value": value} for name, value in writes],
                "password": self.bios_password,
            },
        )
        self._cache = None  # force a re-read so verification sees the truth
        self._value_cache = {}

        results = data.get("results")
        if isinstance(results, dict):
            results = [results]
        if not results:
            transport = str(data.get("error") or "").strip()
            raise CctkError(
                "The BIOS did not answer the write request"
                + (f": {transport}" if transport else ".")
                + " No setting was changed.",
                output=transport,
            )

        for item in results:
            if not isinstance(item, dict):
                continue
            attribute = str(item.get("attribute") or "?")
            value = next((v for n, v in writes if n == attribute), "")
            status = item.get("status")
            try:
                status = int(status) if status is not None else None
            except (TypeError, ValueError):
                status = None
            problem = status_to_exception(
                status,
                attribute=attribute,
                value=value,
                output=str(item.get("error") or ""),
            )
            if problem is not None:
                raise problem

    def _set_attribute(self, attribute: str, value: str) -> None:
        self._write_attributes([(attribute, value)])

    def set_profile(self, profile: ThermalProfile) -> None:
        self._set_attribute(C.ATTR_THERMAL, profile.token)

    def set_charge_config(self, config: ChargeConfig) -> None:
        entry = find_attribute(self._entries(), CHARGE_ATTRIBUTE_CANDIDATES)
        attribute = str(entry.get("AttributeName")) if entry else C.ATTR_PRIMARY_BATT

        if config.mode.needs_thresholds:
            # Thresholds first, mode last: the mode write is what makes the
            # firmware adopt them, so a failure part-way through cannot leave the
            # machine in Custom mode with stale thresholds.
            writes: list[tuple[str, str]] = [
                (CUSTOM_START_ATTRIBUTE, str(config.start)),
                (CUSTOM_STOP_ATTRIBUTE, str(config.stop)),
                (attribute, config.mode.token),
            ]
        else:
            writes = [(attribute, config.token)]
        self._write_attributes(writes)
