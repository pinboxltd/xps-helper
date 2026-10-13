"""Dell Command | Configure (``cctk``) control backend.

Why cctk rather than reverse-engineering Dell's WMI/ACPI calls
--------------------------------------------------------------
Dell's own supported path for "change BIOS settings from the OS" is the
Command | Configure CLI, and it is the same mechanism the community GUI
projects use. It is documented, versioned by Dell, and does not require us to
ship a kernel driver. The trade-off is a dependency on cctk being installed,
which the app states plainly rather than hiding.

The parsing functions at the bottom of this module are deliberately pure: all
of the risk in this file is string handling of Dell's output, so that is what
the tests pin down.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

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


@dataclass(frozen=True)
class CctkResult:
    """The outcome of one cctk invocation."""

    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == C.CCTK_EXIT_OK

    @property
    def combined(self) -> str:
        return f"{self.stdout}\n{self.stderr}".strip()


#: Injectable so tests can drive the backend with canned Dell output.
Runner = Callable[[Sequence[str], int], CctkResult]


def default_runner(argv: Sequence[str], timeout: int) -> CctkResult:
    """Run cctk as a subprocess.

    Uses an argv list with ``shell=False``: the BIOS password is passed as its
    own argument, so it is never re-parsed by a shell.
    """
    log.debug("running: %s", " ".join(argv))
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, no shell
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise BackendUnavailableError(f"Could not execute {argv[0]!r}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CctkError(f"cctk timed out after {timeout}s", output=str(exc)) from exc
    return CctkResult(
        argv=tuple(argv),
        exit_code=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
    )


class CctkBackend:
    """Control backend driven by the cctk executable."""

    name = "cctk"

    def __init__(
        self,
        path: Path | None = None,
        *,
        runner: Runner | None = None,
        bios_password: str | None = None,
        timeout: int = 45,
    ) -> None:
        self.path = Path(path) if path else self.locate()
        self._runner = runner or default_runner
        # Never persist this; read from the environment by default.
        self.bios_password = bios_password or os.environ.get(C.ENV_BIOS_PASSWORD) or None
        self.timeout = timeout
        self._capability_cache: Capabilities | None = None

    # -- discovery --------------------------------------------------------
    @staticmethod
    def locate() -> Path | None:
        """Find cctk: PATH first (so a custom install wins), then Dell's defaults."""
        found = shutil.which("cctk") or shutil.which("cctk.exe")
        if found:
            return Path(found)
        for candidate in C.cctk_candidates():
            if candidate.is_file():
                return candidate
        return None

    @property
    def available(self) -> bool:
        return self.path is not None and Path(self.path).is_file()

    def _require(self) -> Path:
        if not self.available:
            raise BackendUnavailableError(
                "Dell Command | Configure (cctk) was not found. Install it from "
                "https://www.dell.com/support/kbdoc/en-us/000178000/dell-command-configure "
                "and re-run 'xps-helper doctor'."
            )
        return Path(self.path)  # type: ignore[arg-type]

    # -- invocation -------------------------------------------------------
    def _invoke(self, args: Sequence[str]) -> CctkResult:
        exe = self._require()
        argv: list[str] = [str(exe)]
        if self.bios_password:
            argv += [f"{C.BIOS_PWD_ARG_SETUP}={self.bios_password}",
                     f"{C.BIOS_PWD_ARG_SYS}={self.bios_password}"]
        argv += list(args)
        return self._runner(argv, self.timeout)

    def _checked(self, args: Sequence[str], *, context: str) -> CctkResult:
        result = self._invoke(args)
        if not result.ok:
            self._raise_for(result, context=context)
        return result

    @staticmethod
    def _raise_for(result: CctkResult, *, context: str) -> None:
        """Translate Dell's exit codes into typed exceptions.

        The password codes matter: they mean "this would work if you
        authenticated", which is a completely different user action from
        "your model does not support this".
        """
        output = result.combined
        if result.exit_code in C.CCTK_PWD_REQUIRED_CODES:
            raise BiosPasswordRequired(
                f"{context}: the BIOS has a password set. Provide it in the app "
                f"settings (or the {C.ENV_BIOS_PASSWORD} environment variable).",
                exit_code=result.exit_code,
                output=output,
            )
        if result.exit_code in C.CCTK_PWD_INVALID_CODES:
            raise BiosPasswordInvalid(
                f"{context}: the BIOS password was rejected.",
                exit_code=result.exit_code,
                output=output,
            )
        if result.exit_code == C.CCTK_EXIT_OWNER_PWD_SET:
            raise BiosPasswordRequired(
                f"{context}: an owner password is set, which blocks this setting.",
                exit_code=result.exit_code,
                output=output,
            )
        lowered = output.lower()
        # Dell phrases this several ways depending on version, so match loosely.
        unsupported_needles = (
            "wmi-acpi",
            "not supported",
            "not support",        # covers "does not support this feature"
            "unsupported",
            "invalid argument",
            "not applicable",
        )
        if any(needle in lowered for needle in unsupported_needles):
            raise AttributeNotSupported(
                f"{context}: this BIOS does not support that setting. Dell said: "
                f"{output.strip() or 'no output'}",
                exit_code=result.exit_code,
                output=output,
            )
        raise CctkError(
            f"{context} failed (cctk exit code {result.exit_code}): "
            f"{output.strip() or 'no output'}",
            exit_code=result.exit_code,
            output=output,
        )

    # -- reads ------------------------------------------------------------
    def version(self) -> str | None:
        try:
            result = self._invoke(["--Version"])
        except (BackendUnavailableError, CctkError):
            return None
        text = result.combined.strip()
        return text.splitlines()[0] if text else None

    def read_attribute(self, attribute: str) -> str | None:
        """Read one BIOS attribute's current value."""
        result = self._invoke([f"--{attribute}"])
        if not result.ok:
            return None
        return parse_attribute_value(result.combined, attribute)

    def read_profile(self) -> ThermalProfile | None:
        return ThermalProfile.from_token(self.read_attribute(C.ATTR_THERMAL) or "")

    def read_charge_config(self) -> ChargeConfig | None:
        return ChargeConfig.from_token(self.read_attribute(C.ATTR_PRIMARY_BATT) or "")

    def read_raw_attribute(self, attribute: str) -> CctkResult:
        """Raw read, for `doctor` diagnostics."""
        return self._invoke([f"--{attribute}"])

    # -- capability probe -------------------------------------------------
    def supported_values(self, attribute: str) -> tuple[dict[str, bool], str]:
        """Ask cctk which values this model accepts for an attribute.

        Returns ``(values, raw_output)`` where ``values`` maps each token to
        whether Dell marked it supported. An empty dict means "could not parse",
        which callers must treat as unknown rather than as "nothing supported".
        """
        result = self._invoke(["-H", f"--{attribute}"])
        return parse_supported_arguments(result.combined), result.combined

    def probe(self, *, use_cache: bool = True) -> Capabilities:
        if use_cache and self._capability_cache is not None:
            return self._capability_cache

        notes: list[str] = []
        caps = Capabilities(control_backend=self.name)
        # Alias the capability's own list. Assigning to a separate local list
        # silently drops every note, which is how "could not parse" warnings
        # would vanish instead of reaching `doctor`.
        notes = caps.notes

        if not self.available:
            caps.notes.append(
                "Dell Command | Configure (cctk) not found; thermal and charge "
                "control are unavailable."
            )
            self._capability_cache = caps
            return caps

        version = self.version()
        if version:
            notes.append(f"cctk: {version}")

        # Thermal profiles ------------------------------------------------
        caps.thermal_profiles = self._resolve_values(
            C.ATTR_THERMAL, ThermalProfile, "thermal modes", notes
        )

        # Battery charge modes -------------------------------------------
        caps.charge_modes = self._resolve_values(
            C.ATTR_PRIMARY_BATT, ChargeMode, "charge modes", notes
        )

        # Advanced charge schedule ---------------------------------------
        adv_values, _ = self.supported_values(C.ATTR_ADV_BATT)
        caps.supports_advanced_schedule = any(adv_values.values()) if adv_values else False

        self._capability_cache = caps
        return caps

    def _resolve_values(self, attribute, enum_cls, description, notes) -> tuple:
        """Work out which of an attribute's values this model accepts.

        Failing open is deliberate. If Dell's help text is formatted in a way we
        do not recognise, reporting *fewer* capabilities than the machine has
        would silently remove a working feature from the UI with no error to
        explain it. Reporting the documented set instead means an unsupported
        choice fails loudly at write time with Dell's own message -- a much
        better failure mode.
        """
        values, raw = self.supported_values(attribute)
        supported = [token for token, ok in values.items() if ok]

        if supported:
            parsed = (
                parse_profile_list(supported)
                if enum_cls is ThermalProfile
                else parse_charge_mode_list(supported)
            )
            if parsed:
                return parsed
            notes.append(
                f"Dell reported {description} we do not recognise "
                f"({', '.join(supported)}); showing the documented set."
            )
        elif values:
            notes.append(
                f"Dell's help lists {description} but marks none as supported; "
                "showing the documented set instead of hiding them."
            )
        else:
            notes.append(
                f"Could not parse the supported {description} list; showing all "
                "documented values. Unsupported ones will fail with a clear error."
            )

        if raw.strip():
            notes.append(f"{attribute} help said: {_first_lines(raw, 4)}")
        return tuple(enum_cls)

    # -- writes -----------------------------------------------------------
    def _write_attribute(self, attribute: str, value: str) -> None:
        self._checked(
            [f"--{attribute}={value}"],
            context=f"Setting {attribute}={value}",
        )

    def set_profile(self, profile: ThermalProfile) -> None:
        self._write_attribute(C.ATTR_THERMAL, profile.token)

    def set_charge_config(self, config: ChargeConfig) -> None:
        self._write_attribute(C.ATTR_PRIMARY_BATT, config.token)

    def set_advanced_charge_schedule(self, value: str) -> None:
        """Set AdvBatteryChargeCfg, e.g. ``Disabled`` or ``Enabled,Mon-09:00/17:00``."""
        self._write_attribute(C.ATTR_ADV_BATT, value)

    def is_elevated(self) -> bool:
        """Whether we are running with the privileges cctk writes need."""
        if sys.platform != "win32":
            return os.geteuid() == 0  # type: ignore[attr-defined]
        try:
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return False


# ---------------------------------------------------------------------------
# Pure parsing helpers -- the risky part, so they are isolated and tested.
# ---------------------------------------------------------------------------
def parse_attribute_value(output: str, attribute: str) -> str | None:
    """Pull ``Attribute=Value`` out of cctk's read output.

    cctk may print several lines; we match the requested attribute
    case-insensitively and return the first non-empty value.
    """
    if not output:
        return None
    wanted = attribute.strip().lower()
    for line in output.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().lstrip("-").strip().lower()
        if key == wanted:
            value = value.strip()
            return value or None
    return None


def _split_argument_payload(payload: str) -> list[str]:
    """Split Dell's argument list, tolerating the separators we have seen.

    The documented form uses ``|``. Some releases/pages use commas or plain
    whitespace, so we degrade in that order rather than silently returning one
    giant token.
    """
    payload = payload.strip()
    if not payload:
        return []
    if "|" in payload:
        return [chunk.strip() for chunk in payload.split("|") if chunk.strip()]
    if "," in payload:
        return [chunk.strip() for chunk in payload.split(",") if chunk.strip()]
    return [chunk.strip() for chunk in payload.split() if chunk.strip()]


def parse_supported_arguments(output: str) -> dict[str, bool]:
    """Parse cctk's ``Arguments:`` help block into ``{token: supported}``.

    Dell marks the values the current model accepts with ``+``, e.g.::

        Arguments:
         optimized+ | quiet+ | cool | ultraperformance+

    Two real-world formats must be handled: the value list may begin on the
    same line as ``Arguments:`` or on the line(s) *below* it. Missing the
    multi-line form would degrade every probe to the fail-open fallback and make
    a working feature look unsupported, so continuation lines are collected
    until a blank line or the next ``Section:`` header.

    The marker convention was taken from the hardware-tested dell-powermanager
    sources; it is the only way to learn per-model support without trial and
    error.
    """
    supported: dict[str, bool] = {}
    if not output:
        return supported
    lines = output.splitlines()

    def collect(start: int) -> tuple[str, int]:
        """Return the full payload starting at ``start`` and the next index."""
        _, _, payload = lines[start].partition(":")
        payload = payload.strip()
        index = start + 1
        # Continue only when the list is evidently incomplete: nothing on the
        # header line, or it trails off with a separator.
        while index < len(lines) and (not payload or payload.endswith(("|", ","))):
            stripped = lines[index].strip()
            if not stripped or stripped.endswith(":"):
                break
            payload = f"{payload} {stripped}".strip()
            index += 1
        return payload, index

    index = 0
    while index < len(lines):
        if "arguments:" in lines[index].lower():
            payload, index = collect(index)
            for chunk in _split_argument_payload(payload):
                is_supported = "+" in chunk
                token = chunk.replace("+", "").strip()
                if token:
                    # First mention wins; Dell lists each value once.
                    supported.setdefault(token, is_supported)
            continue
        index += 1
    return supported


def _first_lines(text: str, count: int) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()][:count]
    return " / ".join(lines)
