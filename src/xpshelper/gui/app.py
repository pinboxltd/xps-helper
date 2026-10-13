"""tkinter window: the G-Helper-style front end.

Why tkinter
-----------
It ships with CPython on Windows, so the app has **zero required dependencies**
and starts instantly -- the two properties that make G-Helper pleasant. The
trade-off is a plainer look than a WinUI/Qt app, which is the right call here.

Threading rule
--------------
Both cctk writes and WMI reads can take hundreds of milliseconds. Every one of
them runs on a worker thread and the result is marshalled back with
``root.after``, because blocking the Tk event loop makes the window freeze -- the
exact "slow and janky" feeling this project exists to avoid.
"""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path
from typing import Callable, Sequence

from .. import constants as C
from ..errors import XpsHelperError
from ..logsetup import setup_logging
from ..model import (
    BatteryStatus,
    ChargeMode,
    SystemStatus,
    ThermalProfile,
)
from ..service import ConfigStore, XpsHelper, make_service

log = logging.getLogger(__name__)

WINDOW_TITLE = f"{C.APP_DISPLAY_NAME} {C.APP_VERSION}"


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without a display)
# ---------------------------------------------------------------------------
def battery_text(battery: BatteryStatus | None) -> str:
    if battery is None:
        return "unavailable"
    bits: list[str] = []
    if battery.percent is not None:
        bits.append(f"{battery.percent}%")
    if battery.plugged_in is not None:
        if battery.charging:
            bits.append("charging")
        else:
            bits.append("on AC" if battery.plugged_in else "on battery")
    if battery.health_percent is not None:
        bits.append(f"health {battery.health_percent}%")
    if battery.minutes_remaining and not battery.plugged_in:
        hours, minutes = divmod(battery.minutes_remaining, 60)
        bits.append(f"{hours}h{minutes:02d}m left" if hours else f"{minutes}m left")
    return " - ".join(bits) or "detected"


def status_rows(status: SystemStatus) -> list[tuple[str, str]]:
    """Label/value pairs for the status panel."""
    rows: list[tuple[str, str]] = [("Battery", battery_text(status.battery))]

    cpu = status.cpu_temperature
    rows.append(
        ("Temperature", f"{cpu.name}: {cpu.celsius:.1f} C" if cpu else "unavailable")
    )

    if status.fans:
        rows.append(("Fan", ", ".join(f"{f.name} {f.rpm} RPM" for f in status.fans)))

    rows.append(
        ("Charge setting", status.charge.describe if status.charge else "unavailable")
    )
    return rows


def profile_options(caps) -> tuple[ThermalProfile, ...]:
    """Profiles to offer. Falls back to all when the probe found none."""
    return caps.thermal_profiles or tuple(ThermalProfile)


def charge_options(caps) -> tuple[ChargeMode, ...]:
    return caps.charge_modes or tuple(ChargeMode)


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------
class HelperWindow:
    def __init__(self, service: XpsHelper, *, use_tray: bool = True) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.service = service
        self.tk = tk
        self.ttk = ttk
        self._closing = False
        self._refresh_in_flight = False
        self._busy = False
        self._photo = None

        self.root = tk.Tk()
        self.root.title(WINDOW_TITLE)
        self.root.minsize(420, 460)

        self._status_var = tk.StringVar(value="")
        self._message_var = tk.StringVar(value="")
        self._profile_var = tk.StringVar(value="")
        self._charge_var = tk.StringVar(value="")
        self._charge_start_var = tk.StringVar(value=str(C.CUSTOM_START_MIN))
        self._charge_stop_var = tk.StringVar(value=str(C.CUSTOM_STOP_MIN + 20))
        self._password_var = tk.StringVar(value="")
        self._reapply_var = tk.BooleanVar(value=service.settings.reapply_on_power_change)
        self._startup_var = tk.BooleanVar(value=service.settings.apply_profile_on_startup)

        self._build()
        self._wire_tray(use_tray)
        self._sync_from_service()
        self._refresh_async()

    # -- construction -----------------------------------------------------
    def _build(self) -> None:
        tk, ttk = self.tk, self.ttk

        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        header = ttk.Label(
            outer,
            text=self._header_text(),
            font=("Segoe UI", 10, "bold"),
            wraplength=400,
            justify="left",
        )
        header.pack(anchor="w")
        ttk.Label(
            outer,
            text=f"control: {getattr(self.service.control, 'name', '?')}   "
                 f"telemetry: {getattr(self.service.telemetry, 'name', '?')}",
            foreground="#666666",
        ).pack(anchor="w", pady=(0, 8))

        # -- performance --------------------------------------------------
        ttk.Label(outer, text="Performance", font=("Segoe UI", 10, "bold")).pack(anchor="w")

        self._profile_frame = ttk.Frame(outer)
        self._profile_frame.pack(anchor="w", fill="x", pady=(2, 2))

        self._profile_buttons: dict[ThermalProfile, ttk.Radiobutton] = {}
        # Deliberately NOT calling capabilities() here: probing spawns cctk and
        # PowerShell subprocesses and would freeze the window before it appears.
        # All options start enabled and unsupported ones are disabled as soon as
        # the first async status refresh returns a capability report.
        for profile in ThermalProfile:
            button = ttk.Radiobutton(
                self._profile_frame,
                text=profile.label,
                value=profile.value,
                variable=self._profile_var,
                command=self._on_profile_selected,
            )
            button.pack(side="left", padx=(0, 8))
            self._profile_buttons[profile] = button

        self._profile_hint = ttk.Label(
            outer, text="", wraplength=400, justify="left", foreground="#666666"
        )
        self._profile_hint.pack(anchor="w", pady=(0, 8))

        ttk.Separator(outer).pack(fill="x", pady=4)

        # -- battery charge -----------------------------------------------
        ttk.Label(outer, text="Battery charge", font=("Segoe UI", 10, "bold")).pack(anchor="w")

        self._charge_frame = ttk.Frame(outer)
        self._charge_frame.pack(anchor="w", fill="x", pady=(2, 2))

        self._charge_buttons: dict[ChargeMode, ttk.Radiobutton] = {}
        for mode in ChargeMode:
            button = ttk.Radiobutton(
                self._charge_frame,
                text=mode.label,
                value=mode.value,
                variable=self._charge_var,
                command=self._on_charge_mode_changed,
            )
            button.pack(anchor="w")
            self._charge_buttons[mode] = button

        thresholds = ttk.Frame(outer)
        thresholds.pack(anchor="w", fill="x", pady=(4, 2))
        ttk.Label(thresholds, text="Custom start %").pack(side="left")
        self._start_spin = ttk.Spinbox(
            thresholds,
            from_=C.CUSTOM_START_MIN,
            to=C.CUSTOM_START_MAX,
            width=5,
            textvariable=self._charge_start_var,
        )
        self._start_spin.pack(side="left", padx=(4, 12))
        ttk.Label(thresholds, text="stop %").pack(side="left")
        self._stop_spin = ttk.Spinbox(
            thresholds,
            from_=C.CUSTOM_STOP_MIN,
            to=C.CUSTOM_STOP_MAX,
            width=5,
            textvariable=self._charge_stop_var,
        )
        self._stop_spin.pack(side="left", padx=(4, 0))

        self._charge_hint = ttk.Label(
            outer, text="", wraplength=400, justify="left", foreground="#666666"
        )
        self._charge_hint.pack(anchor="w", pady=(2, 0))

        self._apply_charge_button = ttk.Button(
            outer, text="Apply charge setting", command=self._on_apply_charge
        )
        self._apply_charge_button.pack(anchor="w", pady=(6, 8))

        ttk.Separator(outer).pack(fill="x", pady=4)

        # -- status --------------------------------------------------------
        ttk.Label(outer, text="Status", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        status_frame = ttk.Frame(outer)
        status_frame.pack(anchor="w", fill="x", pady=(2, 6))
        self._status_label = ttk.Label(
            status_frame, textvariable=self._status_var, justify="left", wraplength=400
        )
        self._status_label.pack(anchor="w")

        # -- options -------------------------------------------------------
        options = ttk.Frame(outer)
        options.pack(anchor="w", fill="x")
        ttk.Checkbutton(
            options,
            text="Re-apply saved settings when the app starts",
            variable=self._startup_var,
            command=self._on_toggle_startup,
        ).pack(anchor="w")
        ttk.Checkbutton(
            options,
            text="Re-apply when AC power changes",
            variable=self._reapply_var,
            command=self._on_toggle_reapply,
        ).pack(anchor="w")

        password_row = ttk.Frame(outer)
        password_row.pack(anchor="w", fill="x", pady=(6, 0))
        ttk.Label(password_row, text="BIOS password").pack(side="left")
        self._password_entry = ttk.Entry(
            password_row, textvariable=self._password_var, show="*", width=18
        )
        self._password_entry.pack(side="left", padx=(6, 6))
        self._password_entry.bind("<Return>", lambda _event: self._on_password_set())
        ttk.Button(password_row, text="Use", width=6, command=self._on_password_set).pack(
            side="left"
        )
        ttk.Label(
            outer,
            text="Only needed if the BIOS has a password. Kept in memory for this "
                 "session and never written to disk.",
            wraplength=400,
            justify="left",
            foreground="#666666",
        ).pack(anchor="w", pady=(2, 6))

        # -- actions -------------------------------------------------------
        actions = ttk.Frame(outer)
        actions.pack(anchor="w", fill="x", pady=(4, 0))
        ttk.Button(actions, text="Refresh", command=self._refresh_async).pack(side="left")
        ttk.Button(actions, text="Doctor report", command=self._show_doctor).pack(
            side="left", padx=6
        )
        ttk.Button(actions, text="Apply saved", command=self._on_apply_saved).pack(side="left")

        self._message_label = ttk.Label(
            outer, textvariable=self._message_var, wraplength=400, justify="left"
        )
        self._message_label.pack(anchor="w", pady=(8, 0))

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _header_text(self) -> str:
        model = self.service.model
        if model:
            return model
        if self.service.is_dell is False:
            return "Not a Dell system (monitoring only)"
        return "System"

    def _wire_tray(self, use_tray: bool) -> None:
        self.tray = None
        if not use_tray:
            return
        from .tray import TrayIcon

        tray = TrayIcon(
            root=self.root,
            service=self.service,
            on_show=self._show_window,
            on_quit=self._quit,
            on_select_profile=self._select_profile,
            on_select_charge=self._select_charge_mode,
        )
        if tray.available and tray.start():
            self.tray = tray

        # The active profile is not known yet, so start with the neutral icon.
        from .icon import make_photo_icon

        self._photo = make_photo_icon(self.root, None)
        if self._photo is not None:
            try:
                self.root.iconphoto(True, self._photo)
            except Exception:  # noqa: BLE001
                pass

    def _apply_capabilities(self, caps) -> None:
        """Disable options the firmware does not support, once we know."""
        if caps is None:
            return
        if caps.thermal_profiles:
            for profile, button in self._profile_buttons.items():
                try:
                    button.configure(
                        state="normal" if profile in caps.thermal_profiles else "disabled"
                    )
                except Exception:  # noqa: BLE001
                    pass
        if caps.charge_modes:
            for mode, button in self._charge_buttons.items():
                try:
                    button.configure(
                        state="normal" if mode in caps.charge_modes else "disabled"
                    )
                except Exception:  # noqa: BLE001
                    pass
        for note in caps.notes:
            log.info("capability: %s", note)

    # -- state sync -------------------------------------------------------
    def _sync_from_service(self) -> None:
        settings = self.service.settings
        profile = settings.as_profile()
        if profile is not None:
            self._profile_var.set(profile.value)
        charge = settings.as_charge_config()
        if charge is not None:
            self._charge_var.set(charge.mode.value)
            if charge.start is not None:
                self._charge_start_var.set(str(charge.start))
            if charge.stop is not None:
                self._charge_stop_var.set(str(charge.stop))
        self._update_hints()
        self._update_threshold_state()

    def _update_hints(self) -> None:
        profile = ThermalProfile.from_token(self._profile_var.get())
        self._profile_hint.configure(text=profile.description if profile else "")
        mode = ChargeMode.from_token(self._charge_var.get())
        self._charge_hint.configure(text=mode.description if mode else "")

    def _update_threshold_state(self) -> None:
        mode = ChargeMode.from_token(self._charge_var.get())
        state = "normal" if (mode is not None and mode.needs_thresholds) else "disabled"
        for widget in (self._start_spin, self._stop_spin):
            try:
                widget.configure(state=state)
            except Exception:  # noqa: BLE001
                pass
        self._apply_charge_button.configure(
            state="normal" if state == "normal" else "disabled"
        )

    def _apply_status(self, status: SystemStatus) -> None:
        self._status_var.set("\n".join(f"{label}: {value}" for label, value in status_rows(status)))
        self._apply_capabilities(status.capabilities)

        profile = status.profile
        if profile is not None:
            self._profile_var.set(profile.value)
        charge = status.charge
        if charge is not None:
            self._charge_var.set(charge.mode.value)
            if charge.start is not None:
                self._charge_start_var.set(str(charge.start))
            if charge.stop is not None:
                self._charge_stop_var.set(str(charge.stop))

        self._update_hints()
        self._update_threshold_state()
        if self.tray is not None:
            self.tray.update_profile(profile)

        for warning in status.warnings:
            self._set_message(warning, severity="warn")

    def _set_message(self, text: str, *, severity: str = "info") -> None:
        colours = {"info": "#333333", "warn": "#8a6d3b", "error": "#a94442", "ok": "#3c763d"}
        self._message_var.set(text)
        try:
            self._message_label.configure(foreground=colours.get(severity, "#333333"))
        except Exception:  # noqa: BLE001
            pass

    # -- async plumbing ---------------------------------------------------
    def _run_async(self, work: Callable[[], object], on_done: Callable[[object, Exception | None], None]) -> None:
        """Run ``work`` off the UI thread, then deliver the result on it."""
        if self._busy:
            self._set_message("Still working on the previous request...", severity="info")
            return
        self._busy = True

        def runner() -> None:
            try:
                result = work()
                error: Exception | None = None
            except Exception as exc:  # noqa: BLE001 - surfaced to the user
                result, error = None, exc
            try:
                self.root.after(0, lambda: self._finish(on_done, result, error))
            except Exception:  # noqa: BLE001 - window may be gone
                pass

        threading.Thread(target=runner, name="xps-helper-work", daemon=True).start()

    def _finish(self, on_done, result, error) -> None:
        self._busy = False
        on_done(result, error)

    def _refresh_async(self) -> None:
        if self._closing:
            return
        if self._refresh_in_flight:
            self._schedule_refresh()
            return
        self._refresh_in_flight = True

        def work():
            return self.service.status()

        def done(result, error) -> None:
            self._refresh_in_flight = False
            if error is None and isinstance(result, SystemStatus):
                self._apply_status(result)
            elif error is not None:
                self._set_message(f"Could not read status: {error}", severity="error")
            self._schedule_refresh()

        self._run_async(work, done)

    def _schedule_refresh(self) -> None:
        if self._closing:
            return
        interval_ms = max(1, self.service.settings.refresh_seconds) * 1000
        try:
            self.root.after(interval_ms, self._refresh_async)
        except Exception:  # noqa: BLE001
            pass

    # -- actions ----------------------------------------------------------
    def _on_profile_selected(self) -> None:
        profile = ThermalProfile.from_token(self._profile_var.get())
        if profile is None:
            return
        self._update_hints()

        def work():
            return self.service.set_profile(profile)

        def done(result, error) -> None:
            if error is not None:
                self._report_error(error)
                return
            self._set_message(f"Performance set to {profile.label}.", severity="ok")
            if self.tray is not None:
                self.tray.update_profile(profile)
                self.tray.notify(f"Performance: {profile.label}")
            self._refresh_async()

        self._run_async(work, done)

    def _select_profile(self, profile: ThermalProfile) -> None:
        """Entry point for tray menu actions."""
        self._profile_var.set(profile.value)
        self._on_profile_selected()

    def _on_charge_mode_changed(self) -> None:
        self._update_hints()
        self._update_threshold_state()

    def _select_charge_mode(self, mode: ChargeMode) -> None:
        self._charge_var.set(mode.value)
        self._on_charge_mode_changed()
        if not mode.needs_thresholds:
            self._on_apply_charge()

    def _on_apply_charge(self) -> None:
        mode = ChargeMode.from_token(self._charge_var.get())
        if mode is None:
            return

        start = stop = None
        if mode.needs_thresholds:
            try:
                start = int(self._charge_start_var.get())
                stop = int(self._charge_stop_var.get())
            except ValueError:
                self._set_message("Thresholds must be whole numbers.", severity="error")
                return

        def work():
            return self.service.set_charge(mode, start=start, stop=stop)

        def done(result, error) -> None:
            if error is not None:
                self._report_error(error)
                return
            self._set_message(f"Charge setting applied: {result.describe}.", severity="ok")
            if self.tray is not None:
                self.tray.notify(f"Charge: {result.describe}")
            self._refresh_async()

        self._run_async(work, done)

    def _on_apply_saved(self) -> None:
        def work():
            return self.service.apply_saved()

        def done(result, error) -> None:
            if error is not None:
                self._report_error(error)
                return
            messages = result or ["Nothing saved to apply."]
            self._set_message("  ".join(messages), severity="ok")
            self._refresh_async()

        self._run_async(work, done)

    def _on_toggle_startup(self) -> None:
        try:
            self.service.update_settings(apply_profile_on_startup=self._startup_var.get())
        except XpsHelperError as exc:
            self._report_error(exc)

    def _on_toggle_reapply(self) -> None:
        try:
            self.service.update_settings(reapply_on_power_change=self._reapply_var.get())
        except XpsHelperError as exc:
            self._report_error(exc)

    def _on_password_set(self) -> None:
        password = self._password_var.get()
        control = self.service.control
        if hasattr(control, "bios_password"):
            control.bios_password = password or None
            # Re-probe: a password may unlock attributes that were previously
            # reported as password-protected.
            self.service.capabilities(refresh=True)
            self._set_message(
                "BIOS password set for this session." if password else "BIOS password cleared.",
                severity="ok",
            )
        else:
            self._set_message("This backend does not use a BIOS password.", severity="warn")

    def _report_error(self, error: Exception) -> None:
        if isinstance(error, XpsHelperError):
            self._set_message(str(error), severity="error")
        else:
            self._set_message(f"Unexpected error: {error}", severity="error")
        log.warning("operation failed: %s", error)

    def _show_doctor(self) -> None:
        from ..doctor import run_doctor

        def work():
            return run_doctor(self.service).render()

        def done(result, error) -> None:
            if error is not None:
                self._report_error(error)
                return
            popup = self.tk.Toplevel(self.root)
            popup.title("Doctor report")
            popup.geometry("720x520")
            text = self.tk.Text(popup, wrap="none", font=("Consolas", 9))
            text.insert("1.0", result or "")
            text.configure(state="disabled")
            text.pack(fill="both", expand=True)
            self.ttk.Button(popup, text="Close", command=popup.destroy).pack(pady=6)

        self._run_async(work, done)

    # -- window lifecycle -------------------------------------------------
    def _show_window(self) -> None:
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except Exception:  # noqa: BLE001
            pass

    def _on_close(self) -> None:
        """Hide to the tray when one exists; otherwise exit."""
        if self.tray is not None:
            try:
                self.root.withdraw()
                self.tray.notify("XPS Helper is still running in the tray.")
                return
            except Exception:  # noqa: BLE001
                pass
        self._quit()

    def _quit(self) -> None:
        self._closing = True
        if self.tray is not None:
            self.tray.stop()
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass

    def run(self) -> None:
        self.root.mainloop()


def run_gui(service: XpsHelper, *, use_tray: bool = True) -> int:
    """Open the window and block until it closes. Returns a process exit code."""
    try:
        import tkinter  # noqa: F401
    except ImportError as exc:
        print(
            f"The GUI needs tkinter, which is not available: {exc}\n"
            "On Debian/Ubuntu install python3-tk; on Windows use the official "
            "python.org installer (which includes it).\n"
            "The command line still works: xps-helper status",
            file=sys.stderr,
        )
        return 1

    try:
        window = HelperWindow(service, use_tray=use_tray)
    except Exception as exc:  # noqa: BLE001 - commonly "no display name"
        print(
            f"Could not open the window: {exc}\n"
            "If this is a headless machine, use the CLI instead: xps-helper status",
            file=sys.stderr,
        )
        return 1

    window.run()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``xps-helper-gui`` script and the packaged GUI exe.

    Accepts the same backend/config flags as the CLI so the GUI can be pointed
    at the mock backend for demos on any machine.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog=f"{C.APP_NAME}-gui",
        description="Tray/window front end for XPS thermal and battery control.",
    )
    parser.add_argument(
        "--backend", choices=("auto", "cctk", "mock", "null"), default="auto",
        help="Backend to use: auto (default), cctk, mock, or null.",
    )
    parser.add_argument("--config", type=Path, default=None, help="Settings file path.")
    parser.add_argument("--no-tray", action="store_true", help="Do not add a tray icon.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    store = ConfigStore(args.config) if args.config else None
    try:
        service = make_service(args.backend, store=store)
    except XpsHelperError as exc:
        print(f"Could not start: {exc}", file=sys.stderr)
        return 1
    return run_gui(service, use_tray=not args.no_tray)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
