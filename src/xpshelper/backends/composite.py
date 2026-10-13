"""Composite control backend: try the native interface first, then fall back.

Why this exists
---------------
There are three ways to change these BIOS settings, and which one works depends
on the machine and on what the user has installed:

1. The firmware's own WMI-ACPI interface (Windows) or the kernel's
   firmware-attributes sysfs class (Linux). No Dell software required.
2. Dell Command | Configure's ``cctk``, if the user happens to have it.
3. Nothing -- in which case the app must say so clearly instead of failing
   cryptically.

Rather than make the caller care, this wrapper probes its candidates in priority
order on first use, remembers which one worked, and aggregates the *reasons* when
none did. Those reasons are what `doctor` shows the user.
"""

from __future__ import annotations

import logging

from ..errors import XpsHelperError
from ..model import Capabilities, ChargeConfig, ThermalProfile

log = logging.getLogger(__name__)


class CompositeControlBackend:
    """Delegates to the first candidate backend that can actually control."""

    def __init__(self, candidates, *, label: str = "auto") -> None:
        self.candidates = [c for c in candidates if c is not None]
        self._label = label
        self._selected = None
        self._capabilities: Capabilities | None = None
        self._notes: list[str] = []

    # -- selection --------------------------------------------------------
    @property
    def name(self) -> str:
        if self._selected is not None:
            return getattr(self._selected, "name", "unknown")
        return self._label

    def selected(self):
        """The backend in use, selecting one if necessary (may be None)."""
        if self._selected is None and self._capabilities is None:
            self.probe()
        return self._selected

    def probe(self, **_kwargs) -> Capabilities:
        if self._capabilities is not None:
            return self._capabilities

        notes: list[str] = []
        best_read_only = None
        best_read_only_caps = None

        for candidate in self.candidates:
            label = getattr(candidate, "name", type(candidate).__name__)
            try:
                caps = candidate.probe()
            except Exception as exc:  # noqa: BLE001 - a broken candidate must not win
                notes.append(f"{label}: probe failed ({exc})")
                continue

            if caps.can_control:
                self._selected = candidate
                self._capabilities = caps
                caps.notes.extend(notes)
                return caps

            # Remember a backend that can at least read, so status still works.
            if best_read_only is None:
                best_read_only = candidate
                best_read_only_caps = caps
            detail = "; ".join(caps.notes) if caps.notes else "no supported settings reported"
            notes.append(f"{label}: {detail}")

        # Nothing can write. Keep the read-only one so monitoring still works.
        self._selected = best_read_only
        self._capabilities = best_read_only_caps or Capabilities(control_backend="none")
        self._capabilities.notes.extend(notes)
        self._notes = notes
        return self._capabilities

    @property
    def can_control(self) -> bool:
        return bool(self.probe().can_control)

    def is_elevated(self) -> bool:
        control = self.selected()
        if control is None or not hasattr(control, "is_elevated"):
            return False
        try:
            return bool(control.is_elevated())
        except Exception:  # noqa: BLE001
            return False

    # -- reads ------------------------------------------------------------
    def read_profile(self) -> ThermalProfile | None:
        control = self.selected()
        if control is None:
            return None
        return control.read_profile()

    def read_charge_config(self) -> ChargeConfig | None:
        control = self.selected()
        if control is None:
            return None
        return control.read_charge_config()

    # -- writes -----------------------------------------------------------
    def _require_control(self):
        caps = self.probe()
        control = self.selected()
        if control is None or not caps.can_control:
            reasons = self._notes or ["no control backend was available"]
            raise XpsHelperError(
                "Cannot change settings on this machine. "
                + " | ".join(reasons)
            )
        return control

    def set_profile(self, profile: ThermalProfile) -> None:
        self._require_control().set_profile(profile)
        # Force a fresh probe so a later read reflects reality.
        self._capabilities = None

    def set_charge_config(self, config: ChargeConfig) -> None:
        self._require_control().set_charge_config(config)
        self._capabilities = None

    def refresh(self) -> None:
        """Drop cached capability state (used after a password is supplied)."""
        self._capabilities = None
        self._selected = None
        for candidate in self.candidates:
            if hasattr(candidate, "_cache"):
                try:
                    candidate._cache = None  # noqa: SLF001 - deliberate cache reset
                except Exception:  # noqa: BLE001
                    pass
