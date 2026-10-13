"""System-tray icon (optional).

``pystray`` is an optional dependency: without it the app still runs as a normal
window. The tray runs on its own thread, so every callback here is marshalled
back onto the tkinter thread via ``root.after`` -- touching widgets from
another thread is the classic way to get random crashes in a Tk app.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

from ..model import ChargeMode, ThermalProfile

log = logging.getLogger(__name__)


class TrayIcon:
    """Thin wrapper over pystray that degrades to a no-op when unavailable."""

    def __init__(
        self,
        *,
        root,
        service,
        on_show: Callable[[], None],
        on_quit: Callable[[], None],
        on_select_profile: Callable[[ThermalProfile], None],
        on_select_charge: Callable[[ChargeMode], None],
    ) -> None:
        self.root = root
        self.service = service
        self._on_show = on_show
        self._on_quit = on_quit
        self._on_select_profile = on_select_profile
        self._on_select_charge = on_select_charge
        self._icon = None
        self._thread: threading.Thread | None = None
        self.current_profile: ThermalProfile | None = None

    # -- availability -----------------------------------------------------
    @staticmethod
    def _load_pystray():
        try:
            import pystray  # noqa: PLC0415

            return pystray
        except ImportError:
            return None

    @property
    def available(self) -> bool:
        return self._load_pystray() is not None

    # -- lifecycle --------------------------------------------------------
    def start(self) -> bool:
        """Create and run the tray icon. Returns True if it started."""
        pystray = self._load_pystray()
        if pystray is None:
            log.info("pystray is not installed; running without a tray icon")
            return False

        from .icon import make_icon_image

        image = make_icon_image(self.current_profile)
        if image is None:
            log.info("Pillow is not installed; running without a tray icon")
            return False

        menu = pystray.Menu(
            pystray.MenuItem("Open XPS Helper", self._dispatch_show, default=True),
            pystray.Menu.SEPARATOR,
            *[
                pystray.MenuItem(
                    profile.label,
                    self._profile_action(profile),
                    checked=self._profile_checked(profile),
                    radio=True,
                    enabled=self._profile_enabled(profile),
                )
                for profile in ThermalProfile
            ],
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Battery: Primarily AC Use (80%)", self._charge_action(ChargeMode.PRIMARILY_AC)),
            pystray.MenuItem("Battery: Adaptive", self._charge_action(ChargeMode.ADAPTIVE)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._dispatch_quit),
        )

        self._icon = pystray.Icon("xps-helper", image, "XPS Helper", menu)
        self._thread = threading.Thread(target=self._run, name="xps-helper-tray", daemon=True)
        self._thread.start()
        return True

    def _run(self) -> None:
        try:
            assert self._icon is not None
            self._icon.run()
        except Exception:  # noqa: BLE001 - the tray must never take the app down
            log.exception("tray icon stopped unexpectedly")

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:  # noqa: BLE001
                pass
            self._icon = None

    def update_profile(self, profile: ThermalProfile | None) -> None:
        """Reflect the active profile in the icon colour and the radio ticks."""
        self.current_profile = profile
        if self._icon is None:
            return
        from .icon import make_icon_image

        image = make_icon_image(profile)
        if image is not None:
            try:
                self._icon.icon = image
            except Exception:  # noqa: BLE001
                pass
        try:
            self._icon.update_menu()
        except Exception:  # noqa: BLE001
            pass

    def notify(self, message: str, title: str = "XPS Helper") -> None:
        if self._icon is None:
            return
        try:
            self._icon.notify(message, title)
        except Exception:  # noqa: BLE001 - unsupported on some platforms
            pass

    # -- menu builders ----------------------------------------------------
    def _profile_action(self, profile: ThermalProfile):
        def action(icon=None, item=None) -> None:
            self._marshal(lambda: self._on_select_profile(profile))

        return action

    def _profile_checked(self, profile: ThermalProfile):
        def checked(item=None) -> bool:
            return self.current_profile == profile

        return checked

    def _profile_enabled(self, profile: ThermalProfile):
        def enabled(item=None) -> bool:
            # Only use an already-computed probe: this runs on the tray thread
            # and must never spawn subprocesses while drawing the menu.
            caps = self.service.cached_capabilities()
            if caps is None or not caps.thermal_profiles:
                return True
            return profile in caps.thermal_profiles

        return enabled

    def _charge_action(self, mode: ChargeMode):
        def action(icon=None, item=None) -> None:
            self._marshal(lambda: self._on_select_charge(mode))

        return action

    def _dispatch_show(self, icon=None, item=None) -> None:
        self._marshal(self._on_show)

    def _dispatch_quit(self, icon=None, item=None) -> None:
        self._marshal(self._on_quit)

    def _marshal(self, callback: Callable[[], None]) -> None:
        """Run ``callback`` on the tkinter thread."""
        try:
            self.root.after(0, callback)
        except Exception:  # noqa: BLE001 - interpreter may be shutting down
            log.debug("could not marshal tray callback onto the UI thread")
