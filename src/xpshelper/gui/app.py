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
from ..backends.composite import CompositeControlBackend
from ..errors import XpsHelperError
from ..logsetup import setup_logging
from ..model import (
    BatteryStatus,
    ChargeMode,
    SystemStatus,
    ThermalProfile,
)
from ..service import ConfigStore, XpsHelper, make_service
from . import theme, widgets

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


#: Display order for the Dell ACPI-WMI thermal-mode radios. Names match
#: ``thermal.THERMAL_MODES``.
THERMAL_MODE_ORDER = ("Balanced", "Cool", "Quiet", "Ultra")


def _normalise(value) -> str:
    """Reduce an enum, token or string to a comparable string.

    ``ThermalProfile`` and ``ChargeMode`` are ``str`` enums, but
    ``ThermalProfile.QUIET == "quiet"`` is False (the value is ``"Quiet"``), so a
    direct comparison against a token silently fails. This uses ``.value`` when
    present and falls back to ``str``.
    """
    inner = getattr(value, "value", value)
    return str(inner)


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
        self._refresh_handle = None
        self._busy = False
        self._photo = None
        # Set once the thermal mode has been read for the first time. The read is
        # a multi-second WMI round trip, so it is not repeated on every poll.
        self._thermal_read_done = False
        # True once the firmware has confirmed it has no ThermalManagement
        # attribute. Keeps the "not available" explanation from being overwritten
        # by a profile description loaded out of settings.

        # G-Helper's palette. Read once at construction: the window is not
        # designed to flip theme live, and re-styling every widget on the fly is
        # what makes this kind of GUI fragile.
        self.palette = theme.resolve(getattr(service.settings, "theme", None))

        self.root = tk.Tk()
        self.root.title(WINDOW_TITLE)
        self.root.minsize(420, 460)
        self.root.configure(bg=self.palette.form_back)

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
        # NOTE: deliberately no _refresh_thermal() here. See _apply_status.

    # -- construction -----------------------------------------------------
    def _build(self) -> None:
        tk, ttk = self.tk, self.ttk
        p = self.palette

        outer = tk.Frame(self.root, bg=p.form_back, padx=theme.WINDOW_PAD,
                         pady=theme.WINDOW_PAD)
        outer.pack(fill="both", expand=True)
        self._outer = outer

        header = tk.Label(
            outer,
            text=self._header_text(),
            font=("Segoe UI", 11, "bold"),
            bg=p.form_back,
            fg=p.fore_main,
            wraplength=400,
            justify="left",
            anchor="w",
        )
        header.pack(fill="x")
        tk.Label(
            outer,
            text=f"control: {getattr(self.service.control, 'name', '?')}   "
                 f"telemetry: {getattr(self.service.telemetry, 'name', '?')}",
            bg=p.form_back,
            fg=p.fore_dim,
            font=("Segoe UI", 8),
            anchor="w",
        ).pack(fill="x", pady=(0, theme.GROUP_GAP))

        # -- thermal mode (Dell ACPI-WMI) ---------------------------------
        #
        # A separate interface from the ThermalManagement BIOS attribute above.
        # This is the one Dell Power Manager uses on models that have no such
        # attribute (this XPS 13 7390 2-in-1 among them), and it is why the
        # Performance buttons above are correctly disabled there.
        self._thermal_header = widgets.SectionHeader(
            outer, text="Thermal mode (Dell ACPI-WMI)", palette=p
        )
        self._thermal_header.pack(fill="x", pady=(0, 4))

        self._thermal_frame = tk.Frame(outer, bg=p.form_back)
        self._thermal_frame.pack(fill="x", pady=(0, 4))

        self._thermal_var = tk.StringVar(value="")
        self._thermal_buttons: dict[str, widgets.ModeButton] = {}
        for name in THERMAL_MODE_ORDER:
            button = widgets.ModeButton(
                self._thermal_frame,
                text=name,
                accent=theme.mode_accent(name),
                palette=p,
                command=lambda value=name: self._select_and_apply_thermal(value),
            )
            button.pack(side="left", padx=(0, 6))
            self._thermal_buttons[name] = button

        self._thermal_hint = widgets.Hint(outer, palette=p)
        self._thermal_hint.pack(fill="x", pady=(0, theme.GROUP_GAP))

        # -- battery charge -----------------------------------------------
        self._charge_header = widgets.SectionHeader(
            outer, text="Battery charge", palette=p
        )
        self._charge_header.pack(fill="x", pady=(0, 4))

        self._charge_frame = tk.Frame(outer, bg=p.form_back)
        self._charge_frame.pack(fill="x", pady=(0, 4))

        # Charge modes are exclusive but there are five of them, so they wrap onto
        # two rows rather than becoming an unreadably wide single row.
        self._charge_buttons: dict[ChargeMode, widgets.ModeButton] = {}
        for index, mode in enumerate(ChargeMode):
            button = widgets.ModeButton(
                self._charge_frame,
                text=mode.label,
                accent=theme.ACCENT_STANDARD,
                palette=p,
                command=lambda value=mode.value: self._select_charge_mode(value),
            )
            button.grid(row=index // 3, column=index % 3, padx=(0, 6), pady=(0, 6),
                        sticky="w")
            self._charge_buttons[mode] = button

        thresholds = tk.Frame(outer, bg=p.form_back)
        thresholds.pack(fill="x", pady=(2, 2))
        tk.Label(thresholds, text="Custom start %", bg=p.form_back, fg=p.fore_main,
                 font=("Segoe UI", 9)).pack(side="left")
        self._start_spin = ttk.Spinbox(
            thresholds,
            from_=C.CUSTOM_START_MIN,
            to=C.CUSTOM_START_MAX,
            width=5,
            textvariable=self._charge_start_var,
        )
        self._start_spin.pack(side="left", padx=(4, 12))
        tk.Label(thresholds, text="stop %", bg=p.form_back, fg=p.fore_main,
                 font=("Segoe UI", 9)).pack(side="left")
        self._stop_spin = ttk.Spinbox(
            thresholds,
            from_=C.CUSTOM_STOP_MIN,
            to=C.CUSTOM_STOP_MAX,
            width=5,
            textvariable=self._charge_stop_var,
        )
        self._stop_spin.pack(side="left", padx=(4, 0))

        self._charge_hint = widgets.Hint(outer, palette=p)
        self._charge_hint.pack(fill="x", pady=(2, 0))

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
        ttk.Button(actions, text="Diagnose WMI", command=self._show_diagnosis).pack(side="left")
        ttk.Button(actions, text="Apply saved", command=self._on_apply_saved).pack(
            side="left", padx=6
        )
        self._update_button = ttk.Button(
            actions, text="Check for updates", command=self._on_check_updates
        )
        self._update_button.pack(side="left", padx=(6, 0))

        self._message_label = ttk.Label(
            outer, textvariable=self._message_var, wraplength=400, justify="left"
        )
        self._message_label.pack(anchor="w", pady=(8, 0))

        # Built lazily: whether we need administrator rights is only known after
        # the first capability probe, and probing here would freeze the window
        # before it ever appeared.
        self._elevate_frame = ttk.Frame(outer)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- elevation --------------------------------------------------------
    def _maybe_offer_elevation(self, caps) -> None:
        """Offer to re-launch raised when the firmware is refusing access.

        On this hardware the BIOS interface exists but is privilege-gated, so
        without this the window would show every control as unavailable with no
        hint that administrator rights are the whole problem.
        """
        ttk = self.ttk
        control = self.service.control
        # The control object is normally a CompositeControlBackend that picks the
        # first backend able to work. The capability that matters
        # (access_denied) lives on the backend it selected, not on the wrapper --
        # and CompositeControlBackend.selected() returns the wrapper when it has
        # not picked one, so unwrap explicitly.
        selected = control
        if isinstance(control, CompositeControlBackend):
            selected = control.selected() or control

        needs = False
        if hasattr(selected, "access_denied"):
            try:
                needs = bool(selected.access_denied())
            except Exception:  # noqa: BLE001 - never break the UI over a probe
                needs = False
        if not needs:
            return
        if getattr(self, "_elevate_offered", False):
            return
        self._elevate_offered = True

        frame = self._elevate_frame
        frame.pack(anchor="w", fill="x", pady=(6, 0))
        ttk.Label(
            frame,
            text="Administrator rights are required to change BIOS settings.\n"
                 "The firmware is refusing access to this app as it is.",
            wraplength=400,
            justify="left",
            foreground="#8a6d3b",
        ).pack(anchor="w")
        ttk.Button(
            frame, text="Restart as administrator", command=self._on_elevate
        ).pack(anchor="w", pady=(4, 0))

    def _on_elevate(self) -> None:
        """Re-launch this window raised, then close the current one."""
        from ..elevate import (
            ElevationDeclined,
            ElevationError,
            elevation_supported,
            is_elevated,
            spawn_elevated,
        )

        if not elevation_supported():
            self._set_message(
                "Self-elevation is only available on Windows. Use sudo on Linux.",
                severity="warn",
            )
            return
        if is_elevated():
            self._set_message("Already running as administrator.", severity="info")
            return

        # argv[0] is the script path, so rebuild the "gui <flags>" form explicitly.
        argv = [sys.argv[0], "gui"]
        if self.service.store is not None and getattr(self.service.store, "path", None):
            argv += ["--config", str(self.service.store.path)]
        if getattr(self.service.control, "name", "") == "mock":
            argv += ["--backend", "mock"]

        self._set_message("Requesting administrator rights (UAC prompt)...", severity="info")
        try:
            spawn_elevated(argv, mode="gui", capture=False, wait=False)
        except ElevationDeclined as exc:
            self._set_message(str(exc), severity="warn")
            return
        except ElevationError as exc:
            self._set_message(f"Could not elevate: {exc}", severity="error")
            return

        # The raised copy owns the controls now; this one steps aside.
        self._quit()

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
        """Disable options the firmware does not support, once we know.

        The old rule was "only touch the buttons when the supported list is
        non-empty", which meant a machine whose BIOS has **no** thermal attribute
        at all (an XPS 13 7390 2-in-1) kept every thermal radio button enabled --
        the list was empty, so nothing was disabled. Clicking one then attempted a
        write the firmware rejected, surfacing a raw error.
        """
        if caps is None:
            return

        # The Performance profile group has been removed. It controlled the same
        # firmware setting as the Thermal mode group, but only over the
        # ThermalManagement BIOS attribute, which many models (including this
        # XPS 13 7390) do not expose. supports_thermal is deliberately ignored
        # now that there is no group to enable or disable.
        if caps.supports_charge is False:
            self._disable_group(self._charge_buttons)
            self._apply_charge_button.configure(state="disabled")
        elif caps.charge_modes:
            self._enable_only(self._charge_buttons, caps.charge_modes)

        for note in caps.notes:
            log.info("capability: %s", note)

    def _disable_group(self, buttons) -> None:
        """Disable every ModeButton in a group."""
        for button in buttons.values():
            try:
                button.set_enabled(False)
            except Exception:  # noqa: BLE001
                pass

    def _enable_only(self, buttons, supported) -> None:
        """Enable only the ModeButtons whose value is supported."""
        for value, button in buttons.items():
            try:
                button.set_enabled(value in supported)
            except Exception:  # noqa: BLE001
                pass

    def _mark_selected(self, buttons, selected) -> None:
        """Show ``selected`` as the active option and clear the rest.

        The accent fill is the only state indicator, so exactly one button may
        ever be marked -- otherwise the window implies two active modes.

        ``selected`` may be a string token, a ``ThermalProfile`` or a
        ``ChargeMode``, and the button keys may be enums or plain strings.
        Everything is normalised before comparing, because
        ``ThermalProfile.QUIET == "quiet"`` is False and would silently leave the
        caller's own button unmarked.
        """
        wanted = _normalise(selected)
        for value, button in buttons.items():
            try:
                button.set_selected(_normalise(value) == wanted)
            except Exception:  # noqa: BLE001
                pass

    def _set_thermal_hint(self, text: str) -> None:
        try:
            self._thermal_hint.set_text(text)
        except Exception:  # noqa: BLE001
            pass

    # -- selection handlers ------------------------------------------------
    def _select_and_apply_thermal(self, name: str) -> None:
        """Mark a thermal mode and apply it. This is what a button click does."""
        self._thermal_var.set(name)
        self._mark_selected(self._thermal_buttons, name)
        self._on_thermal_mode_selected(name)

    def _select_charge_mode(self, token: str) -> None:
        """Mark a charge mode without applying it.

        Charge is applied by the explicit button, because the custom thresholds
        have to be filled in first.
        """
        self._charge_var.set(token)
        self._mark_selected(self._charge_buttons, token)
        try:
            mode = ChargeMode(token)
        except ValueError:
            return
        self._mark_selected(self._charge_buttons, mode)
        self._on_charge_mode_changed()

    def _disable_thermal_group(self) -> None:
        for button in self._thermal_buttons.values():
            try:
                button.configure(state="disabled")
            except Exception:  # noqa: BLE001
                pass

    # -- thermal mode (Dell ACPI-WMI) -------------------------------------
    def _on_thermal_mode_selected(self, target: str | None = None) -> None:
        """Apply a thermal mode, reverting the button if it is refused.

        ``target`` is passed explicitly rather than read from ``_thermal_var``:
        the variable is a leftover from when these were radio buttons, and
        reading it here meant a click could act on an empty value.
        """
        target = target or self._thermal_var.get()
        if not target:
            return
        self._set_thermal_hint(f"Applying {target}...")

        def done(result, error) -> None:
            if error is not None:
                self._set_thermal_hint(f"Could not set {target}: {error}")
                self._refresh_thermal()
                return
            self._apply_thermal_result(result)

        # If the worker slot is busy the write never starts, so put the button
        # back rather than leaving it showing a mode that was not applied.
        if not self._run_async(lambda: self.service.set_thermal_mode(target), done):
            self._refresh_thermal()

    def _apply_thermal_result(self, result) -> None:
        """Refresh the mode buttons from a ThermalTables report."""
        if result is None:
            return
        try:
            current = result.current_mode_name
            supported = set(result.supported_modes)
        except Exception:  # noqa: BLE001
            return
        self._enable_only(self._thermal_buttons, supported)
        if current:
            self._thermal_var.set(current)
        self._mark_selected(self._thermal_buttons, current)
        if not supported:
            self._set_thermal_hint("The firmware reports no thermal modes.")
        else:
            self._set_thermal_detail(current, result.supported_modes)

    def _set_thermal_detail(self, current: str | None, supported) -> None:
        """Describe the active mode: what it does and what it costs in watts.

        The wattages are reported figures, not measurements -- the firmware
        exposes no power value -- so the text says so rather than implying the
        number was read from this machine.
        """
        from .. import thermal as thermal_mod

        if not current:
            self._set_thermal_hint("Available: " + ", ".join(supported) + ".")
            return
        detail = thermal_mod.THERMAL_MODE_DETAILS.get(current, {})
        if not detail:
            self._set_thermal_hint("Available: " + ", ".join(supported) + ".")
            return
        self._set_thermal_hint(
            f"{current} -- {detail['power']}. Fans: {detail['fans']}. "
            f"{detail['effect']}"
        )

    def _refresh_thermal(self) -> None:
        """Read the thermal mode without blocking the UI thread."""
        def done(result, error) -> None:
            if error is not None:
                self._set_thermal_hint(f"Thermal mode unavailable: {error}")
                self._disable_thermal_group()
                return
            self._apply_thermal_result(result)

        self._run_read(self.service.read_thermal_mode, done)

    # -- state sync -------------------------------------------------------
    def _sync_from_service(self) -> None:
        settings = self.service.settings
        charge = settings.as_charge_config()
        if charge is not None:
            self._charge_var.set(charge.mode.value)
            self._mark_selected(self._charge_buttons, charge.mode)
            if charge.start is not None:
                self._charge_start_var.set(str(charge.start))
            if charge.stop is not None:
                self._charge_stop_var.set(str(charge.stop))
        self._update_hints()
        self._update_threshold_state()

    def _update_hints(self) -> None:
        """Show the selected charge mode's description."""
        mode = ChargeMode.from_token(self._charge_var.get())
        self._charge_hint.set_text(mode.description if mode else "")

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
        self._maybe_offer_elevation(status.capabilities)

        profile = status.profile
        if profile is not None:
            self._active_profile = profile
        charge = status.charge
        if charge is not None:
            self._charge_var.set(charge.mode.value)
            self._mark_selected(self._charge_buttons, charge.mode)
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

        # The thermal read is started here, not in __init__. Every async job goes
        # through _run_async, which refuses to start while _busy is set, so
        # calling it right after _refresh_async() silently dropped it -- the mode
        # buttons then never showed which mode was active. Chaining it off the
        # first completed refresh means the slot is free.
        #
        # Once only: a thermal read is a WMI round trip costing several seconds,
        # and the mode only changes when this window or Dell's own app changes it.
        if not self._thermal_read_done:
            self._thermal_read_done = True
            self._refresh_thermal()

    def _set_message(self, text: str, *, severity: str = "info") -> None:
        colours = {"info": "#333333", "warn": "#8a6d3b", "error": "#a94442", "ok": "#3c763d"}
        self._message_var.set(text)
        try:
            self._message_label.configure(foreground=colours.get(severity, "#333333"))
        except Exception:  # noqa: BLE001
            pass

    # -- async plumbing ---------------------------------------------------
    def _run_async(
        self,
        work: Callable[[], object],
        on_done: Callable[[object, Exception | None], None],
        *,
        background_poll: bool = False,
    ) -> bool:
        """Run ``work`` off the UI thread, then deliver the result on it.

        Returns whether the work was actually started.

        There is one worker slot, and two very different kinds of caller:

        * a **user action** (a click) must be honoured. Dropping it is the worst
          outcome available, because the button has already redrawn itself as
          selected while nothing happened -- which looks like the setting
          silently failing.
        * a **background poll** is optional. The periodic status refresh runs
          every few seconds and holds the slot for the whole time, so with one
          shared slot a click landing during a poll was refused. That is exactly
          how the thermal buttons appeared dead.

        So a poll yields to whatever else is running and simply waits for its next
        tick; a user action is never refused by a poll.
        """
        if self._busy:
            if background_poll:
                log.debug("skipping a background poll; the worker slot is in use")
                return False
            # A user action. Tell them, and do not pretend it started.
            self._set_message(
                "Still finishing the previous request; try again in a moment.",
                severity="warn",
            )
            return False
        self._busy = True

        def runner() -> None:
            try:
                result = work()
                error: Exception | None = None
            except Exception as exc:  # noqa: BLE001 - surfaced to the user
                result, error = None, exc
            try:
                self.root.after(0, lambda: self._finish(on_done, result, error))
            except Exception as exc:  # noqa: BLE001 - window may be gone
                # Reaching here means the result can never be delivered, so the
                # busy flag must be cleared by hand -- otherwise the window is
                # wedged forever, silently refusing every later request with
                # "Still working on the previous request...".
                self._busy = False
                log.warning("could not deliver result to the UI thread: %s", exc)

        threading.Thread(target=runner, name="xps-helper-work", daemon=True).start()
        return True

    def _finish(self, on_done, result, error) -> None:
        self._busy = False
        on_done(result, error)

    def _run_read(
        self,
        work: Callable[[], object],
        on_done: Callable[[object, Exception | None], None],
    ) -> None:
        """Run a read-only job without claiming the action slot.

        Reads are frequent and slow -- the initial capability probe takes about
        8 seconds and a thermal read about 5 -- and they change nothing. Letting
        them hold the single worker slot meant a click could arrive while a read
        was in flight and be refused, which the user experiences as a button that
        does nothing. Reads therefore do not serialise against anything; only
        writes and settings changes claim the slot.
        """

        def runner() -> None:
            try:
                result = work()
                error: Exception | None = None
            except Exception as exc:  # noqa: BLE001 - surfaced to the user
                result, error = None, exc
            try:
                self.root.after(0, lambda: on_done(result, error))
            except Exception as exc:  # noqa: BLE001 - window may be gone
                log.warning("could not deliver a read result to the UI thread: %s", exc)

        threading.Thread(target=runner, name="xps-helper-read", daemon=True).start()

    def _refresh_async(self) -> None:
        if self._closing:
            return
        if self._refresh_in_flight:
            self._schedule_refresh()
            return
        self._refresh_in_flight = True

        # Only the first refresh re-probes the firmware. A probe means enumerating
        # every BIOS attribute, which on the tested laptop costs about 8 seconds;
        # repeating that on each periodic tick would leave the window permanently
        # busy and never settled. Capabilities cannot change while the app is
        # running anyway.
        first_probe = self.service.cached_capabilities() is None

        def work():
            return self.service.status(refresh_caps=first_probe)

        def done(result, error) -> None:
            self._refresh_in_flight = False
            if error is None and isinstance(result, SystemStatus):
                self._apply_status(result)
            elif error is not None:
                self._set_message(f"Could not read status: {error}", severity="error")
            self._schedule_refresh()

        self._run_async(work, done, background_poll=True)

    def _schedule_refresh(self) -> None:
        if self._closing:
            return
        interval_ms = max(1, self.service.settings.refresh_seconds) * 1000
        try:
            # Kept so shutdown can cancel it: a callback still queued when the
            # window is destroyed raises inside Tk's callback dispatcher.
            self._refresh_handle = self.root.after(interval_ms, self._refresh_async)
        except Exception:  # noqa: BLE001
            pass

    # -- actions ----------------------------------------------------------
    def _select_profile(self, profile: ThermalProfile) -> None:
        """Entry point for tray menu actions."""
    def _on_charge_mode_changed(self) -> None:
        self._update_hints()
        self._update_threshold_state()

    def _select_charge_mode(self, mode: ChargeMode) -> None:
        self._charge_var.set(mode.value)
        self._mark_selected(self._charge_buttons, mode)
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
            message = str(error)
            # Point at the deep diagnostic when the problem is the BIOS interface
            # itself: that report says exactly why it was not found.
            if "BIOSAttributeInterface" in message or "Cannot change settings" in message:
                message += "  Use 'Diagnose WMI' for a detailed report."
            self._set_message(message, severity="error")
        else:
            self._set_message(f"Unexpected error: {error}", severity="error")
        log.warning("operation failed: %s", error)

    def _show_text_window(self, title: str, text: str) -> None:
        """Display a block of monospace text in a scrollable window."""
        popup = self.tk.Toplevel(self.root)
        popup.title(title)
        popup.geometry("860x600")
        frame = self.ttk.Frame(popup)
        frame.pack(fill="both", expand=True)
        scrollbar = self.ttk.Scrollbar(frame)
        scrollbar.pack(side="right", fill="y")
        widget = self.tk.Text(
            frame, wrap="none", font=("Consolas", 9), yscrollcommand=scrollbar.set
        )
        widget.insert("1.0", text or "(no output)")
        widget.configure(state="disabled")
        widget.pack(fill="both", expand=True)
        scrollbar.configure(command=widget.yview)
        self.ttk.Button(popup, text="Close", command=popup.destroy).pack(pady=6)

    def _on_check_updates(self) -> None:
        """Ask GitHub whether a newer release exists, off the UI thread.

        Only reports. Installing would mean replacing the running executable,
        which on Windows is the sort of thing that half-succeeds, so the download
        and the release page are handed to the user instead.
        """
        from .. import updates

        def done(result, error) -> None:
            try:
                self._update_button.configure(state="normal")
            except Exception:  # noqa: BLE001
                pass
            if error is not None:
                # "Could not check" is not "up to date"; say which it is.
                self._set_message(
                    f"Could not check for updates: {error}", severity="warn"
                )
                return
            if result is None:
                self._set_message(
                    f"XPS Helper {C.APP_VERSION} is the latest release.", severity="ok"
                )
                return
            self._set_message(
                f"Version {result.version} is available. Opening the release page...",
                severity="ok",
            )
            try:
                import webbrowser

                webbrowser.open(result.url)
            except Exception as exc:  # noqa: BLE001
                log.warning("could not open the release page: %s", exc)
                self._set_message(
                    f"Version {result.version} is available: {result.url}",
                    severity="info",
                )

        try:
            self._update_button.configure(state="disabled")
        except Exception:  # noqa: BLE001
            pass
        self._set_message("Checking for updates...", severity="info")
        self._run_read(updates.check, done)

    def _show_doctor(self) -> None:
        from ..doctor import run_doctor

        def work():
            return run_doctor(self.service).render()

        def done(result, error) -> None:
            if error is not None:
                self._report_error(error)
                return
            self._show_text_window("Doctor report", result or "")

        self._run_async(work, done)

    def _show_diagnosis(self) -> None:
        """Explain exactly which Dell BIOS interface this machine exposes."""
        from ..backends.wmi_diagnose import render as render_diagnosis
        from ..backends.wmi_diagnose import run_diagnosis

        self._set_message("Querying WMI interfaces...", severity="info")

        def work():
            return render_diagnosis(run_diagnosis())

        def done(result, error) -> None:
            if error is not None:
                self._report_error(error)
                return
            self._set_message("WMI diagnosis complete.", severity="ok")
            self._show_text_window("WMI diagnosis", result or "")

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
        # Cancel the pending refresh first: a queued callback that fires after the
        # widgets are destroyed prints a traceback from inside Tk.
        handle = getattr(self, "_refresh_handle", None)
        if handle is not None:
            try:
                self.root.after_cancel(handle)
            except Exception:  # noqa: BLE001
                pass
            self._refresh_handle = None
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
    parser.add_argument(
        "--elevate", action="store_true",
        help="Re-launch the GUI as administrator (Windows UAC prompt).",
    )
    args = parser.parse_args(argv)

    setup_logging(args.verbose)

    if args.elevate:
        from ..elevate import (
            ELEVATE_FLAG,
            ElevationDeclined,
            ElevationError,
            elevation_supported,
            is_elevated,
            spawn_elevated,
        )

        if not elevation_supported():
            print("error: --elevate is only implemented on Windows.", file=sys.stderr)
            return 1
        if is_elevated():
            print(f"note: already running elevated, ignoring {ELEVATE_FLAG}.", file=sys.stderr)
        else:
            raw = [sys.argv[0], *[a for a in (argv or sys.argv[1:]) if a != ELEVATE_FLAG]]
            print("Requesting administrator rights (a UAC prompt will appear)...", file=sys.stderr)
            try:
                spawn_elevated(raw, mode="gui", capture=False, wait=False)
            except ElevationDeclined as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            except ElevationError as exc:
                print(f"error: could not elevate: {exc}", file=sys.stderr)
                return 1
            return 0

    store = ConfigStore(args.config) if args.config else None
    try:
        service = make_service(args.backend, store=store)
    except XpsHelperError as exc:
        print(f"Could not start: {exc}", file=sys.stderr)
        return 1
    return run_gui(service, use_tray=not args.no_tray)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
