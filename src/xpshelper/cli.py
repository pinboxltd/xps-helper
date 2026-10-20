"""Command-line interface.

Every operation the GUI can perform is also available here, which matters for
two reasons: it makes the app scriptable (Task Scheduler, hotkeys, batch files),
and it makes the hardware layer testable without a display.

Exit codes are meaningful so scripts can branch:
    0  success
    1  runtime failure (unsupported value, write did not stick, ...)
    2  usage error (argparse)
    3  a BIOS password is required or was rejected
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from . import constants as C
from .doctor import run_doctor
from .elevate import (
    ELEVATE_FLAG,
    ElevationDeclined,
    ElevationError,
    describe as describe_elevation,
    elevation_supported,
    is_elevated,
    spawn_elevated,
)
from .errors import (
    BiosPasswordInvalid,
    BiosPasswordRequired,
    XpsHelperError,
)
from .logsetup import setup_logging
from .model import ChargeMode, ThermalProfile
from .service import ConfigStore, XpsHelper, make_service

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_BIOS_PASSWORD = 3

BACKEND_CHOICES = ("auto", "native", "cctk", "mock", "null")


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------
def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, sort_keys=False))


def _render_status(service: XpsHelper, status) -> str:
    lines: list[str] = []
    header = f"{C.APP_DISPLAY_NAME} {C.APP_VERSION}"
    if status.model:
        header += f"  --  {status.model}"
    lines.append(header)
    lines.append(f"backends: {service.describe_backend()}")
    lines.append("")

    caps = status.capabilities
    if status.profile is not None:
        profile_text = status.profile.label
        if status.profile.caps_cpu_power:
            profile_text += "  (CPU power capped)"
    else:
        profile_text = "(unavailable)" if not (caps and caps.thermal_profiles) else "(unknown)"
    lines.append(f"Thermal profile : {profile_text}")

    lines.append(f"Charge          : {status.charge.describe if status.charge else '(unavailable)'}")

    if status.battery is not None:
        battery = status.battery
        bits: list[str] = []
        if battery.percent is not None:
            bits.append(f"{battery.percent}%")
        if battery.plugged_in is not None:
            bits.append("on AC" if battery.plugged_in else "on battery")
            if battery.charging:
                bits[-1] = "charging" if battery.plugged_in else bits[-1]
        if battery.health_percent is not None:
            bits.append(f"health {battery.health_percent}%")
        if battery.minutes_remaining and not battery.plugged_in:
            hours, minutes = divmod(battery.minutes_remaining, 60)
            bits.append(f"{hours}h{minutes:02d}m left" if hours else f"{minutes}m left")
        lines.append(f"Battery         : {', '.join(bits) or 'detected'}")
    else:
        lines.append("Battery         : (unavailable)")

    cpu = status.cpu_temperature
    if cpu is not None:
        extra = ""
        others = [t for t in status.temperatures if t is not cpu]
        if others:
            extra = "  (" + ", ".join(f"{t.name} {t.celsius:.0f}C" for t in others[:3]) + ")"
        lines.append(f"Temperature     : {cpu.name} {cpu.celsius:.1f} C{extra}")
    else:
        lines.append("Temperature     : (unavailable)")

    if status.fans:
        lines.append(
            "Fans            : "
            + ", ".join(f"{f.name} {f.rpm} RPM" for f in status.fans)
        )

    if caps is not None:
        if caps.thermal_profiles:
            lines.append(
                "Profiles avail. : "
                + ", ".join(p.label for p in caps.thermal_profiles)
            )
        if caps.charge_modes:
            lines.append(
                "Charge modes    : " + ", ".join(m.label for m in caps.charge_modes)
            )

    if status.warnings:
        lines.append("")
        for warning in status.warnings:
            lines.append(f"warning: {warning}")
    return "\n".join(lines)


def _render_profiles(service: XpsHelper, current: ThermalProfile | None) -> str:
    caps = service.capabilities()
    supported = caps.thermal_profiles
    lines = ["Thermal profiles:"]

    if caps.supports_thermal is False:
        # The attribute does not exist, so nothing here can be set. Saying so once
        # is clearer than marking four rows unsupported without explanation.
        for profile in ThermalProfile:
            lines.append(f"  - {profile.value:<17} {profile.label}")
        lines.append("")
        lines.append(
            "  None of these can be set: this BIOS exposes no thermal-profile "
            "attribute.\n  Battery charge control is unaffected -- see 'charge --list'."
        )
        return "\n".join(lines)

    for profile in ThermalProfile:
        available = profile in supported if supported else True
        if not available:
            marker = "  -"
        elif profile == current:
            marker = "  *"
        else:
            marker = "   "
        suffix = "" if available else "  (not supported by this BIOS)"
        lines.append(f"{marker} {profile.value:<17} {profile.label}{suffix}")
    lines.append("")
    lines.append(f"  * = active. Set with: {C.APP_NAME} profile <name>")
    return "\n".join(lines)


def _render_charge(service: XpsHelper, current) -> str:
    caps = service.capabilities()
    supported = caps.charge_modes
    lines = ["Battery charge configuration:"]
    for mode in ChargeMode:
        available = mode in supported if supported else True
        if not available:
            marker = "  -"
        elif current is not None and mode == current.mode:
            marker = "  *"
        else:
            marker = "   "
        suffix = "" if available else "  (not supported by this BIOS)"
        lines.append(f"{marker} {mode.value:<13} {mode.label}{suffix}")
    if current is not None:
        lines.append("")
        lines.append(f"  Active: {current.describe}")
    lines.append("")
    lines.append(
        f"  * = active. Set with: {C.APP_NAME} charge <mode> "
        f"[--start {C.CUSTOM_START_MIN} --stop {C.CUSTOM_STOP_MIN}]"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def _cmd_status(args: argparse.Namespace, service: XpsHelper) -> int:
    if args.watch:
        interval = max(1, args.interval)
        try:
            while True:
                status = service.status()
                if args.json:
                    _print_json(status.to_dict())
                else:
                    sys.stdout.write("\033[2J\033[H")  # clear screen
                    print(_render_status(service, status))
                    print(f"\n(refresh every {interval}s -- Ctrl+C to stop)")
                time.sleep(interval)
        except KeyboardInterrupt:
            return EXIT_OK
    status = service.status()
    if args.json:
        _print_json(status.to_dict())
    else:
        print(_render_status(service, status))
    return EXIT_OK


def _cmd_profile(args: argparse.Namespace, service: XpsHelper) -> int:
    current = None
    try:
        current = service.control.read_profile()
    except Exception:  # noqa: BLE001 - readback is best-effort for display
        pass

    if args.list or not args.name:
        if args.json:
            _print_json(
                {
                    "current": current.value if current else None,
                    "supported": [p.value for p in service.capabilities().thermal_profiles],
                    "available": [p.value for p in ThermalProfile],
                }
            )
        else:
            print(_render_profiles(service, current))
        return EXIT_OK

    applied = service.set_profile(args.name, verify=not args.no_verify)
    if args.json:
        _print_json({"applied": applied.value, "label": applied.label})
    else:
        print(f"Thermal profile set to {applied.label}.")
        if applied.caps_cpu_power:
            print("Note: this profile caps sustained CPU power (~11 W on XPS 13).")
    return EXIT_OK


def _cmd_internal_thermal(args: argparse.Namespace, service: XpsHelper) -> int:
    """Hidden: read or set the thermal mode and print JSON. For the helper child.

    Exists so the privileged child can be started as an ordinary subcommand. The
    obvious alternative, ``python -c "<snippet>"``, cannot work in a frozen build:
    ``sys.executable`` is then the app rather than the interpreter, so ``-c`` is
    meaningless and the snippet is parsed as a subcommand name.
    """
    from . import thermal as thermal_mod

    try:
        if args.mode:
            tables = thermal_mod.set_thermal_mode(args.mode)
        else:
            tables = thermal_mod.read_thermal_mode()
    except thermal_mod.ThermalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(json.dumps(tables.as_dict()))
    return EXIT_OK


def _cmd_sensors(args: argparse.Namespace, service: XpsHelper) -> int:
    """Report the temperature sensors.

    On this class of machine the only live source is the Intel DPTF zones in
    ``root\\wmi :: EsifDeviceInformation``, which return data **only when
    elevated**. The packaged executable is elevated by its manifest; a source run
    reaches this through the privileged helper, which is what ``--esif-only``
    exists for -- it keeps the helper's job to one query.
    """
    if args.esif_only:
        from .backends.wmi import WmiTelemetryBackend

        backend = WmiTelemetryBackend()
        backend.refresh()
        # refresh() may have used the helper already; read the cache directly so
        # --esif-only cannot recurse.
        raw = backend._snapshot()  # noqa: SLF001 - same package
        print(json.dumps({"esif_zones": list(raw.esif_zones)}))
        return EXIT_OK

    try:
        readings = service.telemetry.read_temperatures()
    except Exception as exc:  # noqa: BLE001
        print(f"error: could not read temperatures: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if args.json:
        _print_json(
            {
                "sensors": [
                    {"name": r.name, "celsius": r.celsius, "source": r.source}
                    for r in readings
                ]
            }
        )
        return EXIT_OK

    if not readings:
        print("No temperature sensors reported a value.")
        print(
            "Reading them needs administrator rights: EsifDeviceInformation returns "
            "nothing unelevated."
        )
        return EXIT_OK

    print(f"{len(readings)} temperature sensor(s), hottest first:")
    for reading in readings:
        print(f"  {reading.name:<30} {reading.celsius:6.1f} C   ({reading.source})")
    return EXIT_OK


def _cmd_update(args: argparse.Namespace, service: XpsHelper) -> int:
    """Check GitHub for a newer release, and offer the download link.

    Never installs anything by itself: the release asset is a zip containing two
    executables, and replacing a running Windows binary from inside itself is a
    good way to leave a broken app behind. The user gets the exact URL instead.
    """
    from . import updates

    try:
        release = updates.check()
    except updates.UpdateCheckError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(
            "Could not check for updates. This is not the same as being up to "
            "date -- try again when the network is available.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    if release is None:
        if args.json:
            _print_json({"current": C.APP_VERSION, "update_available": False})
        else:
            print(f"XPS Helper {C.APP_VERSION} is the latest release.")
        return EXIT_OK

    if args.json:
        _print_json(
            {
                "current": C.APP_VERSION,
                "update_available": True,
                "latest": release.version,
                "url": release.url,
                "download": release.asset_url,
            }
        )
    else:
        print(f"A newer release is available: {C.APP_VERSION} -> {release.version}")
        print(f"  release notes : {release.url}")
        if release.asset_url:
            print(f"  download      : {release.asset_url}")
            print("  Unzip it over the existing folder, then run 'xps-helper "
                  "helper --install --force' so the scheduled task points at the "
                  "new copy.")
    return EXIT_OK


def _cmd_thermal(args: argparse.Namespace, service: XpsHelper) -> int:
    """Read or set the thermal mode over Dell's ACPI-WMI interface.

    This is the separate interface Dell Power Manager uses on models with no
    ``ThermalManagement`` BIOS attribute, and the only way to change the performance
    mode on them.
    """
    from . import thermal as thermal_mod

    try:
        if args.mode:
            tables = service.set_thermal_mode(args.mode)
            if args.json:
                _print_json(tables.as_dict())
            else:
                name = tables.current_mode_name or tables.current_mode
                print(f"Thermal mode applied: {name}.")
            return EXIT_OK
        tables = service.read_thermal_mode()
    except thermal_mod.ThermalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except XpsHelperError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if args.json:
        _print_json(tables.as_dict())
    else:
        current = tables.current_mode_name
        print(f"Current thermal mode : {current}" if current else
              f"Current thermal mode : {tables.current_mode} (unrecognised)")
        available = ", ".join(tables.supported_modes) or "none reported"
        print(f"Available            : {available}")
        if tables.fan_failure:
            print(f"Fan failure          : {tables.fan_failure}")
        print()
        print(_render_thermal_modes(tables))
    return EXIT_OK


def _render_thermal_modes(tables) -> str:
    """Describe every available mode, marking which one is active."""
    from . import thermal as thermal_mod

    lines: list[str] = []
    for name in THERMAL_MODE_ORDER_FOR_CLI:
        if name not in tables.supported_modes:
            continue
        detail = thermal_mod.THERMAL_MODE_DETAILS.get(name, {})
        marker = "*" if name == tables.current_mode_name else " "
        lines.append(f"{marker} {name}")
        if detail.get("power"):
            lines.append(f"    power : {detail['power']}")
        if detail.get("fans"):
            lines.append(f"    fans  : {detail['fans']}")
        if detail.get("effect"):
            lines.append(f"    {detail['effect']}")
    if lines:
        lines.append("")
        lines.append("* = active. " + thermal_mod.THERMAL_MODE_CAVEAT)
    return "\n".join(lines)


#: Display order for the mode description block.
THERMAL_MODE_ORDER_FOR_CLI = ("Quiet", "Cool", "Balanced", "Ultra")


def _cmd_charge(args: argparse.Namespace, service: XpsHelper) -> int:
    current = None
    try:
        current = service.control.read_charge_config()
    except Exception:  # noqa: BLE001
        pass

    if args.list or not args.mode:
        if args.json:
            _print_json(
                {
                    "current": current.to_dict() if current else None,
                    "supported": [m.value for m in service.capabilities().charge_modes],
                    "available": [m.value for m in ChargeMode],
                }
            )
        else:
            print(_render_charge(service, current))
        return EXIT_OK

    applied = service.set_charge(
        args.mode, start=args.start, stop=args.stop, verify=not args.no_verify
    )
    if args.json:
        _print_json(applied.to_dict())
    else:
        print(f"Battery charge setting applied: {applied.describe}.")
        if applied.mode is ChargeMode.EXPRESS:
            print("Note: Express Charge generates more heat and wears the battery faster.")
        if applied.mode is ChargeMode.PRIMARILY_AC:
            print("Note: this caps charging around 80%, which extends battery lifespan.")
    return EXIT_OK


def _cmd_doctor(args: argparse.Namespace, service: XpsHelper) -> int:
    report = run_doctor(service, include_raw=not args.no_raw)
    if args.json:
        _print_json(report.to_dict())
    else:
        print(report.render())
    return EXIT_ERROR if report.has_failures else EXIT_OK


def _cmd_settings(args: argparse.Namespace, service: XpsHelper) -> int:
    changes: dict[str, Any] = {}
    if args.refresh_seconds is not None:
        changes["refresh_seconds"] = max(1, args.refresh_seconds)
    for flag, key in (
        ("apply_profile_on_startup", "apply_profile_on_startup"),
        ("apply_charge_on_startup", "apply_charge_on_startup"),
        ("reapply_on_power_change", "reapply_on_power_change"),
    ):
        value = getattr(args, flag, None)
        if value is not None:
            changes[key] = value

    if changes:
        service.update_settings(**changes)

    settings = service.settings
    data = settings.to_dict()
    if args.json:
        _print_json(data)
    else:
        print(f"Settings file: {service.store.path}")
        print(f"  profile                  : {settings.profile or '(none saved)'}")
        print(
            "  charge                   : "
            + (settings.as_charge_config().describe if settings.as_charge_config() else "(none saved)")
        )
        print(f"  refresh_seconds          : {settings.refresh_seconds}")
        print(f"  apply_profile_on_startup : {settings.apply_profile_on_startup}")
        print(f"  apply_charge_on_startup  : {settings.apply_charge_on_startup}")
        print(f"  reapply_on_power_change  : {settings.reapply_on_power_change}")
    return EXIT_OK


def _cmd_apply(args: argparse.Namespace, service: XpsHelper) -> int:
    messages = service.apply_saved()
    if args.json:
        _print_json({"messages": messages})
    else:
        for message in messages or ["Nothing to apply (no saved profile or charge setting)."]:
            print(message)
    return EXIT_OK


def _cmd_gui(args: argparse.Namespace, service: XpsHelper) -> int:
    try:
        from .gui.app import run_gui
    except ImportError as exc:  # pragma: no cover - depends on host tooling
        print(f"The GUI needs tkinter, which is unavailable: {exc}", file=sys.stderr)
        return EXIT_ERROR
    return run_gui(service, use_tray=not args.no_tray)


def _cmd_helper(args: argparse.Namespace, service: XpsHelper) -> int:
    """Manage the privileged helper that removes the per-use UAC prompt."""
    from . import privileged as priv
    from .elevate import elevation_supported

    if args.uninstall:
        if not priv.is_installed():
            print("No privileged helper is installed.")
            return EXIT_OK
        output = priv.run_elevated_once(priv.uninstall_script())
        print(output.strip() or "Privileged helper removed.")
        return EXIT_OK

    if args.install:
        if not elevation_supported():
            print("error: the privileged helper is Windows-only.", file=sys.stderr)
            return EXIT_ERROR
        if priv.is_installed() and not args.force:
            print(
                "The privileged helper is already installed. Re-run with --force to "
                "replace it."
            )
            return EXIT_OK
        print(
            "Installing the privileged helper. This is the ONE time a UAC prompt is "
            "needed; after this, privileged actions run without prompting.",
            file=sys.stderr,
        )
        try:
            output = priv.run_elevated_once(priv.install_script())
        except priv.HelperError as exc:
            # Say why. Printing a bare "installed: False" was actively misleading:
            # it looked like a silent failure when in fact the UAC prompt had been
            # dismissed or the script had errored.
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_ERROR
        if output.strip():
            print(output.strip())
        installed = priv.is_installed()
        if not installed:
            print(
                "error: the install script reported success but no task was "
                "registered. Run with -v for details.",
                file=sys.stderr,
            )
            return EXIT_ERROR
        print("installed: True")
        print(
            "Privileged actions now run without a UAC prompt. "
            "Remove it any time with 'xps-helper helper --uninstall'."
        )
        return EXIT_OK

    # Status.
    state = priv.describe()
    if args.json:
        _print_json(state)
    else:
        print(f"privileged helper : {'installed' if state['installed'] else 'not installed'}")
        print(f"task name         : {state['task_name']}")
        # Report the *registered* action when there is one: that is what decides
        # whether the helper actually works, and it differs from what installing
        # now would produce as soon as the app has been moved.
        if state["installed"] and state.get("registered_command"):
            print(f"registered action : {state['registered_command']}")
            if state.get("stale"):
                print(
                    "warning           : that path no longer exists. Reinstall with "
                    "'xps-helper helper --install --force'."
                )
        else:
            print(f"action            : {' '.join(state['command'])}")
        print(f"note              : {state['note']}")
    return EXIT_OK


def _cmd_privileged_run(args: argparse.Namespace, service: XpsHelper) -> int:
    """Run an arbitrary command through the installed privileged helper.

    A general escape hatch, and the reason it exists: some diagnostics need
    elevation to read at all (Dell's ACPI-WMI classes return "Access denied"
    otherwise), and re-launching elevated per probe means a UAC prompt every time.
    Going through the helper keeps that to the single install prompt.
    """
    from . import privileged as priv

    command = list(args.command_args or [])
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("error: give a command after '--'.", file=sys.stderr)
        return EXIT_USAGE

    try:
        result = priv.run_via_helper(command)
    except priv.HelperNotInstalled as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except priv.HelperError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    return EXIT_OK if result.ok else EXIT_ERROR


def _cmd_diagnose_wmi(args: argparse.Namespace, service: XpsHelper) -> int:
    """Deep WMI diagnosis: which Dell interfaces exist, and why one failed."""
    from .backends.wmi_diagnose import render as render_diagnosis
    from .backends.wmi_diagnose import run_diagnosis, to_json

    diagnosis = run_diagnosis(timeout=args.timeout)
    if args.json:
        print(to_json(diagnosis))
    else:
        print(render_diagnosis(diagnosis))
    return EXIT_OK if not diagnosis.get("error") else EXIT_ERROR


def _cmd_capabilities(args: argparse.Namespace, service: XpsHelper) -> int:
    caps = service.capabilities(refresh=True)

    # Dell's second thermal interface, if any. This is the one that matters on
    # models with no ThermalManagement BIOS attribute.
    thermal_caps = None
    try:
        from . import thermal

        thermal_caps = thermal.probe()
    except Exception as exc:  # noqa: BLE001 - a probe must not break the command
        log.debug("thermal capability probe failed: %s", exc)

    if args.json:
        payload = caps.to_dict()
        payload["dell_thermal_interface"] = (
            thermal_caps.to_dict() if thermal_caps is not None else None
        )
        _print_json(payload)
    else:
        print(f"control backend   : {caps.control_backend}")
        print(f"telemetry backend : {caps.telemetry_backend}")
        print(f"thermal profiles  : {', '.join(p.value for p in caps.thermal_profiles) or 'none'}")
        print(f"charge modes      : {', '.join(m.value for m in caps.charge_modes) or 'none'}")
        print(f"custom thresholds : {'yes' if caps.supports_custom_thresholds else 'no'}")
        print(f"advanced schedule : {'yes' if caps.supports_advanced_schedule else 'no'}")
        print(f"battery / temps / fans : {caps.battery_readable} / "
              f"{caps.temperatures_readable} / {caps.fans_readable}")
        if thermal_caps is not None:
            state = {
                True: "present",
                False: "absent",
                None: "unknown",
            }.get(thermal_caps.acpi_wmi_supported, "unknown")
            classes = ", ".join(
                f"DA_CLASS_{c:02d}" for c in thermal_caps.supported_da_classes
            )
            print(f"Dell thermal iface: {state}"
                  + (f" ({classes})" if classes else ""))
            print("                    (what Dell Power Manager uses; XPS Helper "
                  "does not write it yet)")
        for note in caps.notes:
            print(f"note: {note}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
def _make_common(*, suppressed: bool) -> argparse.ArgumentParser:
    """Build a fresh set of the flags shared by the top level and subcommands.

    Why a factory and not a single shared ``common`` parser
    ----------------------------------------------------------------
    ``ArgumentParser.set_defaults()`` does not just record defaults -- it
    *mutates* ``action.default`` on the matching actions. And ``parents=[...]``
    shares the very same ``Action`` objects with every subparser. So calling
    ``set_defaults(backend="auto")`` on the top-level parser also rewrote the
    subparsers' action default, and because argparse copies the subparser's
    namespace back over the parent's, ``--backend mock`` given *before* the
    subcommand was silently reset to ``"auto"``.

    Handing each parser its own independent actions avoids the aliasing
    entirely. Verified by test_cli.py::test_global_flags_before_subcommand.
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--backend",
        choices=BACKEND_CHOICES,
        default=argparse.SUPPRESS if suppressed else "auto",
        help="Backend to use: auto (default), cctk, mock, or null.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=argparse.SUPPRESS if suppressed else None,
        help="Path to the settings JSON file (default: per-user config dir).",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        default=argparse.SUPPRESS if suppressed else False,
        help="Enable debug logging.",
    )
    parser.add_argument(
        ELEVATE_FLAG,
        action="store_true",
        default=argparse.SUPPRESS if suppressed else False,
        help=(
            "Re-run this command as administrator (Windows UAC prompt). Needed to "
            "*change* BIOS settings; not needed for 'status'."
        ),
    )
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=C.APP_NAME,
        description=(
            "Thermal profile and battery charge control for Dell XPS laptops, "
            "in the spirit of G-Helper."
        ),
        parents=[_make_common(suppressed=False)],
        epilog=(
            "Examples:\n"
            f"  {C.APP_NAME} status\n"
            f"  {C.APP_NAME} profile quiet\n"
            f"  {C.APP_NAME} charge custom --start 60 --stop 80\n"
            f"  {C.APP_NAME} charge primarily-ac\n"
            f"  {C.APP_NAME} doctor\n"
            f"  {C.APP_NAME} --backend mock gui\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"{C.APP_NAME} {C.APP_VERSION}")

    subparsers = parser.add_subparsers(dest="command")

    status = subparsers.add_parser(
        "status", parents=[_make_common(suppressed=True)], help="Show current profile, charge setting and sensors."
    )
    status.add_argument("--json", action="store_true", help="Emit JSON.")
    status.add_argument("--watch", action="store_true", help="Refresh continuously.")
    status.add_argument(
        "--interval", type=int, default=5, help="Seconds between refreshes in --watch mode."
    )
    status.set_defaults(func=_cmd_status)

    profile = subparsers.add_parser(
        "profile", parents=[_make_common(suppressed=True)], help="Get or set the thermal profile."
    )
    profile.add_argument(
        "name", nargs="?",
        help="optimized | quiet | cool | ultra-performance (aliases accepted).",
    )
    profile.add_argument("--list", action="store_true", help="List supported profiles.")
    profile.add_argument("--json", action="store_true", help="Emit JSON.")
    profile.add_argument(
        "--no-verify", action="store_true",
        help="Do not read the value back after writing.",
    )
    profile.set_defaults(func=_cmd_profile)

    charge = subparsers.add_parser(
        "charge", parents=[_make_common(suppressed=True)], help="Get or set the battery charge configuration."
    )
    charge.add_argument(
        "mode", nargs="?",
        help="standard | express | primarily-ac | adaptive | custom (aliases accepted).",
    )
    charge.add_argument("--start", type=int, help="Custom mode: start charging at this %%.")
    charge.add_argument("--stop", type=int, help="Custom mode: stop charging at this %%.")
    charge.add_argument("--list", action="store_true", help="List supported charge modes.")
    charge.add_argument("--json", action="store_true", help="Emit JSON.")
    charge.add_argument("--no-verify", action="store_true", help="Skip read-back verification.")
    charge.set_defaults(func=_cmd_charge)

    thermal = subparsers.add_parser(
        "thermal", parents=[_make_common(suppressed=True)],
        help="Read or set the performance mode via Dell's ACPI-WMI interface.",
    )
    thermal.add_argument(
        "mode", nargs="?",
        help="Thermal mode to apply: Balanced, Cool, Quiet or Ultra. Omit to read.",
    )
    thermal.add_argument("--json", action="store_true", help="Emit JSON.")
    thermal.set_defaults(func=_cmd_thermal)

    update = subparsers.add_parser(
        "update", parents=[_make_common(suppressed=True)],
        help="Check GitHub for a newer release.",
    )
    update.add_argument("--json", action="store_true", help="Emit JSON.")
    update.set_defaults(func=_cmd_update)

    sensors = subparsers.add_parser(
        "sensors", parents=[_make_common(suppressed=True)],
        help="Report temperature sensors (Intel DPTF zones).",
    )
    sensors.add_argument("--json", action="store_true", help="Emit JSON.")
    sensors.add_argument(
        "--esif-only", action="store_true",
        help="Internal: print only the raw DPTF zones, for the privileged child.",
    )
    sensors.set_defaults(func=_cmd_sensors)

    # Hidden: the privileged helper's entry point for thermal work. Not listed in
    # help, and it takes no service, because it runs in the elevated child. The
    # name must not start with "-": argparse would treat it as an option rather
    # than a subcommand.
    internal = subparsers.add_parser(
        "internal-thermal", add_help=False, help=argparse.SUPPRESS
    )
    internal.add_argument("mode", nargs="?")
    internal.set_defaults(func=_cmd_internal_thermal)

    doctor = subparsers.add_parser(
        "doctor", parents=[_make_common(suppressed=True)],
        help="Diagnose which features work on this machine (run this first).",
    )
    doctor.add_argument("--json", action="store_true", help="Emit JSON.")
    doctor.add_argument(
        "--no-raw", action="store_true", help="Omit raw Dell command output."
    )
    doctor.set_defaults(func=_cmd_doctor)

    capabilities = subparsers.add_parser(
        "capabilities", parents=[_make_common(suppressed=True)], help="Show what the firmware supports."
    )
    capabilities.add_argument("--json", action="store_true", help="Emit JSON.")
    capabilities.set_defaults(func=_cmd_capabilities)

    diagnose = subparsers.add_parser(
        "diagnose-wmi", parents=[_make_common(suppressed=True)],
        help="Deep WMI diagnosis: which Dell BIOS interfaces exist on this machine.",
    )
    diagnose.add_argument("--json", action="store_true", help="Emit JSON.")
    diagnose.add_argument(
        "--timeout", type=int, default=120, help="Seconds to allow for the WMI queries."
    )
    diagnose.set_defaults(func=_cmd_diagnose_wmi)

    settings = subparsers.add_parser(
        "settings", parents=[_make_common(suppressed=True)], help="View or change persisted settings."
    )
    settings.add_argument("--refresh-seconds", type=int, help="GUI refresh interval.")
    settings.add_argument(
        "--apply-profile-on-startup", action=argparse.BooleanOptionalAction, default=None,
        help="Re-apply the saved thermal profile when the app starts.",
    )
    settings.add_argument(
        "--apply-charge-on-startup", action=argparse.BooleanOptionalAction, default=None,
        help="Re-apply the saved charge configuration when the app starts.",
    )
    settings.add_argument(
        "--reapply-on-power-change", action=argparse.BooleanOptionalAction, default=None,
        help="Re-apply saved settings when AC power is connected/disconnected.",
    )
    settings.add_argument("--json", action="store_true", help="Emit JSON.")
    settings.set_defaults(func=_cmd_settings, refresh_seconds=None)

    apply_cmd = subparsers.add_parser(
        "apply", parents=[_make_common(suppressed=True)], help="Re-apply saved settings now."
    )
    apply_cmd.add_argument("--json", action="store_true", help="Emit JSON.")
    apply_cmd.set_defaults(func=_cmd_apply)

    gui = subparsers.add_parser("gui", parents=[_make_common(suppressed=True)], help="Launch the tray GUI.")
    gui.add_argument("--no-tray", action="store_true", help="Do not add a tray icon.")
    gui.set_defaults(func=_cmd_gui)

    helper = subparsers.add_parser(
        "helper", parents=[_make_common(suppressed=True)],
        help="Install/remove the privileged helper that avoids a UAC prompt per use.",
    )
    helper.add_argument("--install", action="store_true", help="Register the helper (one UAC prompt).")
    helper.add_argument("--uninstall", action="store_true", help="Remove the helper.")
    helper.add_argument("--force", action="store_true", help="Replace an existing helper.")
    helper.add_argument("--json", action="store_true", help="Emit JSON status.")
    helper.set_defaults(func=_cmd_helper)

    priv_run = subparsers.add_parser(
        "privileged-run", parents=[_make_common(suppressed=True)],
        help="Run a command through the privileged helper (no UAC prompt).",
    )
    priv_run.add_argument(
        "command_args", nargs=argparse.REMAINDER,
        help="Command to run, e.g. privileged-run -- python -c \"print(1)\"",
    )
    priv_run.set_defaults(func=_cmd_privileged_run)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    # Remember the vector before parsing: --elevate has to re-run this exact
    # command, and argparse does not preserve ordering or spelling.
    raw_argv = list(sys.argv) if argv is None else [C.APP_NAME, *argv]
    args = parser.parse_args(argv)
    setup_logging(getattr(args, "verbose", False))

    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_USAGE

    if getattr(args, "elevate", False):
        return _reexec_elevated(raw_argv)

    return _dispatch(args)


def _dispatch(args: argparse.Namespace) -> int:
    """Build the service and run the chosen command, mapping errors to codes."""
    store = ConfigStore(args.config) if getattr(args, "config", None) else None
    try:
        service = make_service(getattr(args, "backend", "auto"), store=store)
        return int(args.func(args, service))
    except BiosPasswordRequired as exc:
        print(f"BIOS password required: {exc}", file=sys.stderr)
        print(
            f"Set the {C.ENV_BIOS_PASSWORD} environment variable, or enter it in the "
            "GUI settings, then retry.",
            file=sys.stderr,
        )
        return EXIT_BIOS_PASSWORD
    except BiosPasswordInvalid as exc:
        print(f"BIOS password rejected: {exc}", file=sys.stderr)
        return EXIT_BIOS_PASSWORD
    except XpsHelperError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        return EXIT_OK


def _reexec_elevated(raw_argv: Sequence[str]) -> int:
    """Re-run this command with administrator rights, forwarding its exit code."""
    if not elevation_supported():
        print("error: --elevate is only implemented on Windows.", file=sys.stderr)
        return EXIT_ERROR
    if is_elevated():
        # Already there; the flag is a no-op rather than a nested UAC prompt.
        print(f"note: already running elevated, ignoring {ELEVATE_FLAG}.", file=sys.stderr)
        return EXIT_OK

    print("Requesting administrator rights (a UAC prompt will appear)...", file=sys.stderr)
    try:
        return spawn_elevated(raw_argv, mode="console")
    except ElevationDeclined as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except ElevationError as exc:
        print(f"error: could not elevate: {exc}", file=sys.stderr)
        print(f"Try: {describe_elevation(raw_argv)}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
