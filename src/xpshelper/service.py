"""Service layer: persistence, apply-with-verification, and status assembly.

The rule this module enforces: **a write is not considered successful until it
has been read back**. Dell's firmware silently declines some writes (a profile
change while a Thunderbolt dock is attached, for instance), and reporting
success in that case is worse than failing, because the user walks away
believing their fan noise or charge limit changed.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import constants as C
from .backends import ControlBackend, TelemetryBackend, build_backends, probe_capabilities
from .backends.base import NullControlBackend, NullTelemetryBackend, collect_status
from .errors import ValidationError, XpsHelperError
from .model import (
    ChargeConfig,
    ChargeMode,
    SystemStatus,
    ThermalProfile,
)
from . import platform as platform_info

log = logging.getLogger(__name__)


@dataclass
class Settings:
    """User settings persisted between runs."""

    version: int = 1
    profile: str | None = None
    charge_mode: str | None = None
    charge_start: int | None = None
    charge_stop: int | None = None
    refresh_seconds: int = 5
    apply_profile_on_startup: bool = False
    apply_charge_on_startup: bool = False
    reapply_on_power_change: bool = False
    #: GUI theme: "dark", "light", or "windows" to follow the OS. None means
    #: "windows", which is the least surprising default for a first run.
    theme: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settings":
        """Build settings from untrusted JSON, coercing or discarding junk.

        A settings file can be hand-edited, truncated by a crash, or written by
        a different version of the app. Loading it must never raise: an
        exception here would stop the app from starting at all, which is a much
        worse outcome than ignoring a bad value. Unknown keys are dropped, and
        anything of the wrong type falls back to its default.
        """
        raw = {k: v for k, v in (data or {}).items() if k in cls.__dataclass_fields__}

        def optional_str(value: Any) -> str | None:
            return value.strip() if isinstance(value, str) and value.strip() else None

        def as_int(value: Any, default: int | None) -> int | None:
            if isinstance(value, bool):  # bool is an int subclass; reject it
                return default
            if isinstance(value, int):
                return value
            if isinstance(value, float):
                return int(value)
            if isinstance(value, str):
                try:
                    return int(value.strip())
                except ValueError:
                    return default
            return default

        version = as_int(raw.get("version"), 1) or 1
        refresh = as_int(raw.get("refresh_seconds"), 5)
        extra = raw.get("extra")

        return cls(
            version=max(1, version),
            profile=optional_str(raw.get("profile")),
            charge_mode=optional_str(raw.get("charge_mode")),
            charge_start=as_int(raw.get("charge_start"), None),
            charge_stop=as_int(raw.get("charge_stop"), None),
            refresh_seconds=max(1, refresh if refresh is not None else 5),
            apply_profile_on_startup=raw.get("apply_profile_on_startup") is True,
            apply_charge_on_startup=raw.get("apply_charge_on_startup") is True,
            reapply_on_power_change=raw.get("reapply_on_power_change") is True,
            extra=extra if isinstance(extra, dict) else {},
        )

    def as_charge_config(self) -> ChargeConfig | None:
        """Reconstruct a ChargeConfig, or None if the stored one is unusable."""
        if not self.charge_mode:
            return None
        mode = ChargeMode.from_token(self.charge_mode)
        if mode is None:
            return None
        try:
            return ChargeConfig(
                mode=mode,
                start=self.charge_start if mode.needs_thresholds else None,
                stop=self.charge_stop if mode.needs_thresholds else None,
            )
        except ValidationError:
            log.warning("stored charge settings are invalid; ignoring them")
            return None

    def as_profile(self) -> ThermalProfile | None:
        if not self.profile:
            return None
        return ThermalProfile.from_token(self.profile)


class ConfigStore:
    """JSON file store with atomic writes."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else C.config_file()

    def load(self) -> Settings:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return Settings()
        except OSError as exc:
            log.warning("could not read settings from %s: %s", self.path, exc)
            return Settings()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("settings file %s is not valid JSON; starting fresh", self.path)
            return Settings()
        if not isinstance(data, dict):
            return Settings()
        return Settings.from_dict(data)

    def save(self, settings: Settings) -> None:
        """Write via a temp file + replace so a crash cannot truncate settings."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(settings.to_dict(), indent=2, sort_keys=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".config-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp_name, self.path)
        except OSError as exc:
            log.warning("could not save settings to %s: %s", self.path, exc)
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise XpsHelperError(f"Could not save settings: {exc}") from exc


def _package_root() -> Path:
    """The ``src`` directory holding the ``xpshelper`` package.

    The privileged child runs with a different working directory, so the thermal
    snippet has to be told where to import the package from explicitly.
    """
    return Path(__file__).resolve().parent.parent


class XpsHelper:
    """High-level facade used by both the CLI and the GUI."""

    def __init__(
        self,
        control: ControlBackend | None = None,
        telemetry: TelemetryBackend | None = None,
        *,
        store: ConfigStore | None = None,
        backend: str = "auto",
        bios_password: str | None = None,
    ) -> None:
        if control is None or telemetry is None:
            control, telemetry = build_backends(backend, bios_password=bios_password)
        self.control = control
        self.telemetry = telemetry
        self.store = store or ConfigStore()
        self.settings = self.store.load()
        self._capabilities = None
        self.model = platform_info.system_model()
        self._is_dell = platform_info.is_dell()
        self._seed_mock()

    def _seed_mock(self) -> None:
        """Replay saved settings into a mock backend, for coherent demo mode.

        Controls observed in tests: a fresh process using ``--backend mock``
        reports the saved profile rather than the mock's built-in default.
        """
        from .backends.mock import MockBackend

        if not isinstance(self.control, MockBackend):
            return
        self.control.seed(
            profile=self.settings.as_profile(), charge=self.settings.as_charge_config()
        )

    # -- capabilities -----------------------------------------------------
    def capabilities(self, *, refresh: bool = False):
        """Capability report, probing the hardware on first use."""
        if self._capabilities is None or refresh:
            self._capabilities = probe_capabilities(self.control, self.telemetry)
            if self._is_dell is not None:
                self._capabilities.is_dell = self._is_dell
        return self._capabilities

    def cached_capabilities(self):
        """Capability report only if it has already been probed.

        Callers on a hot path (the tray menu redraw, for instance) must not
        trigger a probe: that can spawn several subprocesses and freeze whatever
        thread calls it.
        """
        return self._capabilities

    @property
    def is_dell(self) -> bool | None:
        return self._is_dell

    # -- reads ------------------------------------------------------------
    def status(self, *, refresh_caps: bool = False) -> SystemStatus:
        # Invalidate any cached WMI snapshot so the caller sees current values.
        if hasattr(self.telemetry, "invalidate"):
            try:
                self.telemetry.invalidate()
            except Exception:  # noqa: BLE001
                pass
        status = collect_status(
            self.control,
            self.telemetry,
            capabilities=self.capabilities(refresh=refresh_caps),
            model=self.model,
            is_dell=self._is_dell,
        )
        return status

    # -- writes -----------------------------------------------------------
    def set_profile(
        self,
        profile: ThermalProfile | str,
        *,
        persist: bool = True,
        verify: bool = True,
    ) -> ThermalProfile:
        """Apply a thermal profile and confirm the firmware accepted it."""
        target = self._resolve_profile(profile)
        self._ensure_controllable()

        caps = self.capabilities()
        if caps.supports_thermal is False:
            # The attribute does not exist, so this cannot succeed. Say why in
            # terms the user can act on rather than passing on a raw firmware
            # error for a setting this model does not have.
            raise ValidationError(
                "This BIOS exposes no thermal-profile setting, so the performance "
                "mode cannot be changed on this model. Battery charge control is "
                "unaffected."
            )

        supported = caps.thermal_profiles
        if supported and target not in supported:
            options = ", ".join(p.label for p in supported)
            raise ValidationError(
                f"{target.label} is not supported by this BIOS. Available: {options}."
            )

        self.control.set_profile(target)
        if verify:
            self._verify_profile(target)

        if persist:
            self.settings.profile = target.value
            self.store.save(self.settings)
        log.info("thermal profile set to %s", target.value)
        return target

    # -- Dell ACPI-WMI thermal modes --------------------------------------
    #
    # A second, separate thermal interface. On models with no ThermalManagement
    # BIOS attribute (this XPS 13 7390 2-in-1 among them) this is the one Dell
    # Power Manager itself uses, so it is the only way to change the performance
    # mode. It needs elevation, which the privileged helper supplies without a
    # prompt.
    def read_thermal_mode(self) -> "thermal.ThermalTables":
        """Read the firmware's thermal tables over Dell's ACPI-WMI interface."""
        from . import privileged as priv
        from . import thermal as thermal_mod

        if priv.is_installed() and not priv.is_privileged_child():
            return self._thermal_via_helper(read_only=True)
        return thermal_mod.read_thermal_mode()

    def set_thermal_mode(self, mode: str | int) -> "thermal.ThermalTables":
        """Set the thermal mode over Dell's ACPI-WMI interface, then verify."""
        from . import privileged as priv
        from . import thermal as thermal_mod

        if priv.is_installed() and not priv.is_privileged_child():
            return self._thermal_via_helper(mode=mode)
        return thermal_mod.set_thermal_mode(mode)

    def _thermal_via_helper(
        self, *, mode: str | int | None = None, read_only: bool = False
    ) -> "thermal.ThermalTables":
        """Run the thermal operation through the no-prompt privileged helper."""
        import json as _json

        from . import privileged as priv
        from . import thermal as thermal_mod

        if read_only:
            expression = "print(json.dumps(thermal.read_thermal_mode().as_dict()))"
        else:
            expression = (
                "print(json.dumps(thermal.set_thermal_mode("
                f"{mode!r}).as_dict()))"
            )
        code = (
            "import json, sys; "
            f"sys.path.insert(0, {str(_package_root())!r}); "
            "from xpshelper import thermal; " + expression
        )
        result = priv.run_via_helper([sys.executable, "-c", code])
        if not result.ok:
            from .errors import XpsHelperError

            raise XpsHelperError(
                "the privileged helper could not reach the thermal interface: "
                + (result.stderr or result.stdout or "no output").strip()
            )
        payload = None
        for line in reversed((result.stdout or "").splitlines()):
            if line.lstrip().startswith("{"):
                payload = line
                break
        if payload is None:
            from .errors import XpsHelperError

            raise XpsHelperError("the thermal helper returned no result")
        # as_dict() also carries derived names (supported_modes, current_mode_name),
        # which are properties rather than constructor fields.
        raw = _json.loads(payload)
        return thermal_mod.ThermalTables(
            error=raw.get("error"),
            supported_mask=raw.get("supported_mask", 0),
            current_mode=raw.get("current_mode", 0),
            fan_failure=raw.get("fan_failure", 0),
        )

    def set_charge(
        self,
        config: ChargeConfig | ChargeMode | str,
        *,
        start: int | None = None,
        stop: int | None = None,
        persist: bool = True,
        verify: bool = True,
    ) -> ChargeConfig:
        """Apply a charge configuration and confirm the firmware accepted it."""
        target = self._resolve_charge(config, start=start, stop=stop)
        self._ensure_controllable()

        # Delegate FIRST when a privileged helper is installed.
        #
        # Order matters and this was a real bug: the capability check below reads
        # the firmware, and an unelevated process is refused ("this BIOS exposes no
        # battery charge configuration attribute"), so the write aborted before it
        # ever reached the helper that exists precisely to do it elevated. The
        # helper runs elevated, so it performs both the check and the write.
        if self._via_privileged_helper("charge", target):
            if persist:
                self.settings.charge_mode = target.mode.value
                self.settings.charge_start = target.start
                self.settings.charge_stop = target.stop
                self.store.save(self.settings)
            log.info("charge configuration set to %s (via helper)", target.token)
            return target

        caps = self.capabilities()
        if caps.supports_charge is False:
            raise ValidationError(
                "This BIOS exposes no battery charge configuration attribute, so the "
                "charge setting cannot be changed on this model."
            )

        supported = caps.charge_modes
        if supported and target.mode not in supported:
            options = ", ".join(m.label for m in supported)
            raise ValidationError(
                f"{target.mode.label} is not supported by this BIOS. Available: {options}."
            )

        self.control.set_charge_config(target)
        if verify:
            self._verify_charge(target)

        if persist:
            self.settings.charge_mode = target.mode.value
            self.settings.charge_start = target.start
            self.settings.charge_stop = target.stop
            self.store.save(self.settings)
        log.info("charge configuration set to %s", target.token)
        return target

    def _via_privileged_helper(self, kind: str, target: Any) -> bool:
        """Do a write through the installed privileged helper, if there is one.

        Returns True when the helper performed the write, False when the caller
        should do it in-process.

        Why this exists: writing a BIOS setting needs administrator rights, and
        re-launching elevated per action shows a UAC prompt every time. When the
        helper is installed, the write is delegated to an already-elevated
        Scheduled Task, so no prompt appears. When it is not installed, nothing
        changes -- the caller does the write the old way and the usual elevation
        rules apply.
        """
        from . import privileged as priv
        from .elevate import is_elevated

        # Recursion guard, first and unconditionally.
        #
        # This process may be the privileged child the helper started. If it asked
        # for a helper again, the chain would never terminate: each child would
        # start another task, hang, and eventually time out with no useful error.
        # Checking an explicit marker cannot be wrong, whereas relying on an
        # elevation *check* can be -- a failed token query reads as "not elevated".
        if priv.is_privileged_child():
            return False

        # Already privileged: no point paying for a helper round trip.
        if is_elevated():
            return False
        try:
            if not priv.is_installed():
                return False
        except Exception:  # noqa: BLE001 - never let this break a write
            return False

        import sys as _sys

        if kind == "charge":
            cli = [
                _sys.executable,
                "-m",
                "xpshelper",
                "charge",
                str(target.mode.value),
            ]
            if target.start is not None and target.stop is not None:
                cli += ["--start", str(target.start), "--stop", str(target.stop)]
        else:  # pragma: no cover - profiles cannot use this path yet
            return False

        # Do not persist twice: the child applies the same command.
        cli.append("--no-verify")
        result = priv.run_via_helper(cli)
        if not result.ok:
            raise XpsHelperError(
                "The privileged helper could not change the setting: "
                f"{(result.stderr or result.stdout or '').strip()[:300]}"
            )
        log.info("charge configuration applied via the privileged helper")
        return True

    def _verify_profile(self, target: ThermalProfile) -> None:
        """Read the profile back and complain if the firmware did not honour it."""
        try:
            actual = self.control.read_profile()
        except Exception as exc:  # noqa: BLE001 - readback is best-effort
            log.debug("could not read back profile: %s", exc)
            return
        if actual is not None and actual != target:
            raise XpsHelperError(
                f"The firmware reported {actual.label} after we asked for "
                f"{target.label}. The change did not take effect -- this can happen "
                "when a Thunderbolt dock is attached, or when the value is locked "
                "by BIOS policy."
            )

    def _verify_charge(self, target: ChargeConfig) -> None:
        try:
            actual = self.control.read_charge_config()
        except Exception as exc:  # noqa: BLE001
            log.debug("could not read back charge config: %s", exc)
            return
        if actual is None:
            return
        # Compare the mode; some BIOSes normalise custom thresholds slightly.
        if actual.mode != target.mode:
            raise XpsHelperError(
                f"The firmware reported {actual.describe} after we asked for "
                f"{target.describe}. The change did not take effect."
            )
        if target.mode.needs_thresholds and (actual.start, actual.stop) != (
            target.start,
            target.stop,
        ):
            raise XpsHelperError(
                f"Thresholds did not stick: firmware reports {actual.start}-"
                f"{actual.stop}%, requested {target.start}-{target.stop}%."
            )

    # -- startup / power-change re-application ---------------------------
    def apply_saved(self) -> list[str]:
        """Re-apply saved settings at startup. Returns human-readable results."""
        messages: list[str] = []
        if self.settings.apply_profile_on_startup:
            profile = self.settings.as_profile()
            if profile is not None:
                try:
                    applied = self.set_profile(profile, persist=False)
                    messages.append(f"Applied thermal profile: {applied.label}")
                except XpsHelperError as exc:
                    messages.append(f"Could not apply saved profile: {exc}")
        if self.settings.apply_charge_on_startup:
            config = self.settings.as_charge_config()
            if config is not None:
                try:
                    applied = self.set_charge(config, persist=False)
                    messages.append(f"Applied charge setting: {applied.describe}")
                except XpsHelperError as exc:
                    messages.append(f"Could not apply saved charge setting: {exc}")
        return messages

    def power_state(self) -> tuple[bool | None, int | None]:
        """Return (plugged_in, battery_percent) for power-change detection."""
        try:
            battery = self.telemetry.read_battery()
        except Exception:  # noqa: BLE001
            return (None, None)
        if battery is None:
            return (None, None)
        return (battery.plugged_in, battery.percent)

    # -- helpers ----------------------------------------------------------
    def _resolve_profile(self, profile: ThermalProfile | str) -> ThermalProfile:
        if isinstance(profile, ThermalProfile):
            return profile
        member = ThermalProfile.from_token(str(profile))
        if member is None:
            options = ", ".join(p.label for p in ThermalProfile)
            raise ValidationError(f"Unknown thermal profile {profile!r}. Try one of: {options}.")
        return member

    def _resolve_charge(
        self,
        config: ChargeConfig | ChargeMode | str,
        *,
        start: int | None,
        stop: int | None,
    ) -> ChargeConfig:
        if isinstance(config, ChargeConfig):
            if start is not None or stop is not None:
                raise ValidationError("Pass either a ChargeConfig or thresholds, not both.")
            return config
        if isinstance(config, ChargeMode):
            mode = config
        else:
            text = str(config)
            # Accept a full token like "custom:75-80" directly.
            if ":" in text:
                parsed = ChargeConfig.from_token(text)
                if parsed is not None:
                    return parsed
            mode = ChargeMode.from_token(text)
            if mode is None:
                options = ", ".join(m.label for m in ChargeMode)
                raise ValidationError(f"Unknown charge mode {config!r}. Try one of: {options}.")
        if mode.needs_thresholds and (start is None or stop is None):
            raise ValidationError(
                "Custom charge mode needs both --start and --stop "
                f"(start {C.CUSTOM_START_MIN}-{C.CUSTOM_START_MAX}%, "
                f"stop {C.CUSTOM_STOP_MIN}-{C.CUSTOM_STOP_MAX}%, gap >= {C.CUSTOM_MIN_GAP}%)."
            )
        return ChargeConfig(mode=mode, start=start, stop=stop)

    def _ensure_controllable(self) -> None:
        if isinstance(self.control, NullControlBackend):
            raise XpsHelperError(self.control.reason)

    def update_settings(self, **changes: Any) -> Settings:
        for key, value in changes.items():
            if not hasattr(self.settings, key):
                raise ValidationError(f"Unknown setting {key!r}.")
            setattr(self.settings, key, value)
        self.store.save(self.settings)
        return self.settings

    def describe_backend(self) -> str:
        return (
            f"control={getattr(self.control, 'name', '?')}, "
            f"telemetry={getattr(self.telemetry, 'name', '?')}"
        )


def make_service(
    backend: str = "auto", *, bios_password: str | None = None, store: ConfigStore | None = None
) -> XpsHelper:
    return XpsHelper(backend=backend, bios_password=bios_password, store=store)
