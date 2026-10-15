"""Tests for the GUI's capability-driven control states.

The bug these guard against was found from a screenshot of the real window on an
XPS 13 7390 2-in-1: every thermal radio button was enabled and clickable on a
machine whose BIOS has no thermal-profile attribute at all, so choosing one
produced a raw firmware error and left the window showing a setting that had
never taken effect.

Deliberately no real Tk objects: creating a Tk root needs a display and a
correctly configured Tcl library, which makes a test suite platform-dependent.
The widget interaction is one ``configure(state=...)`` call per button, so a
stand-in records exactly what the real widget would have been told.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from xpshelper.errors import ValidationError
from xpshelper.gui.app import THERMAL_MODE_ORDER, HelperWindow
from xpshelper.model import Capabilities, ChargeMode, ThermalProfile


class FakeWidget:
    """Records the options ``configure`` was called with."""

    def __init__(self, **options) -> None:
        self.options = dict(options)
        self.configure_calls = 0

    def configure(self, **options) -> None:
        self.configure_calls += 1
        self.options.update(options)

    def cget(self, key):
        return self.options.get(key)

    def set_text(self, text: str) -> None:
        """The themed Hint widget's API, replacing ``configure(text=...)``."""
        self.configure(text=text)

    def set_palette(self, _palette) -> None:  # pragma: no cover - theming only
        pass

    @property
    def state(self):
        return self.options.get("state")

    @property
    def text(self):
        return self.options.get("text")


class FakeButton(FakeWidget):
    """Stand-in for :class:`xpshelper.gui.widgets.ModeButton`.

    The real widget draws on a Canvas, so it needs a Tk root. These tests stay
    display-free by recording the same calls, and mirror the real API
    (``set_enabled`` / ``set_selected``) rather than the old ttk ``configure``.
    """

    def __init__(self, text: str = "") -> None:
        super().__init__(text=text, state="normal")
        self._enabled = True
        self._selected = False

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        self.options["state"] = "normal" if enabled else "disabled"

    def set_selected(self, selected: bool) -> None:
        self._selected = bool(selected)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def selected(self) -> bool:
        return self._selected

    def set_text(self, text: str) -> None:
        self.options["text"] = text

    def pack(self, **_kwargs) -> None:  # pragma: no cover - layout only
        pass

    def grid(self, **_kwargs) -> None:  # pragma: no cover - layout only
        pass


def _window(caps) -> SimpleNamespace:
    """A stand-in exposing only what ``_apply_capabilities`` touches."""
    window = SimpleNamespace(
        _profile_buttons={p: FakeButton(p.label) for p in ThermalProfile},
        _charge_buttons={m: FakeButton(m.label) for m in ChargeMode},
        _profile_hint=FakeWidget(text=""),
        _apply_charge_button=FakeButton("Apply charge setting"),
    )
    # Bind the real implementations so the test exercises production logic.
    window._disable_group = HelperWindow._disable_group.__get__(window)
    window._enable_only = HelperWindow._enable_only.__get__(window)
    window._mark_selected = HelperWindow._mark_selected.__get__(window)
    window._set_profile_hint = HelperWindow._set_profile_hint.__get__(window)
    window._apply_capabilities = HelperWindow._apply_capabilities.__get__(window)
    return window


def _states(buttons) -> dict:
    return {key: button.state for key, button in buttons.items()}


# ---------------------------------------------------------------------------
# Thermal
# ---------------------------------------------------------------------------
def test_absent_thermal_attribute_disables_every_profile_button():
    """The exact machine state: charge works, thermal does not exist."""
    caps = Capabilities(
        control_backend="wmi-bios",
        supports_thermal=False,
        supports_charge=True,
        charge_modes=(
            ChargeMode.ADAPTIVE,
            ChargeMode.STANDARD,
            ChargeMode.EXPRESS,
            ChargeMode.PRIMARILY_AC,
            ChargeMode.CUSTOM,
        ),
    )
    window = _window(caps)
    window._apply_capabilities(caps)

    assert set(_states(window._profile_buttons).values()) == {"disabled"}
    # And the hint explains why, in terms the user can act on.
    assert "no thermal-profile setting" in window._profile_hint.text
    # Charge control must be left fully usable.
    assert set(_states(window._charge_buttons).values()) == {"normal"}


def test_present_thermal_attribute_leaves_only_supported_profiles_enabled():
    caps = Capabilities(
        control_backend="wmi-bios",
        supports_thermal=True,
        thermal_profiles=(ThermalProfile.OPTIMIZED, ThermalProfile.QUIET),
    )
    window = _window(caps)
    window._apply_capabilities(caps)

    states = _states(window._profile_buttons)
    assert states[ThermalProfile.OPTIMIZED] == "normal"
    assert states[ThermalProfile.QUIET] == "normal"
    assert states[ThermalProfile.COOL] == "disabled"
    assert states[ThermalProfile.ULTRA_PERFORMANCE] == "disabled"


def test_unreadable_value_list_fails_open():
    """supports_thermal None means "could not tell" -- do not hide the feature."""
    caps = Capabilities(control_backend="wmi-bios", supports_thermal=None)
    window = _window(caps)
    window._apply_capabilities(caps)

    assert set(_states(window._profile_buttons).values()) == {"normal"}


# ---------------------------------------------------------------------------
# Charge
# ---------------------------------------------------------------------------
def test_absent_charge_attribute_disables_charge_controls():
    caps = Capabilities(control_backend="wmi-bios", supports_charge=False)
    window = _window(caps)
    window._apply_capabilities(caps)

    assert set(_states(window._charge_buttons).values()) == {"disabled"}
    assert window._apply_charge_button.state == "disabled"


def test_supported_charge_modes_disable_only_the_rest():
    caps = Capabilities(
        control_backend="wmi-bios",
        supports_charge=True,
        charge_modes=(ChargeMode.STANDARD, ChargeMode.CUSTOM),
    )
    window = _window(caps)
    window._apply_capabilities(caps)

    states = _states(window._charge_buttons)
    assert states[ChargeMode.STANDARD] == "normal"
    assert states[ChargeMode.CUSTOM] == "normal"
    assert states[ChargeMode.EXPRESS] == "disabled"
    assert states[ChargeMode.PRIMARILY_AC] == "disabled"
    assert states[ChargeMode.ADAPTIVE] == "disabled"


def test_no_capabilities_are_touched_before_a_probe():
    """caps=None means "not probed yet" -- leave the controls alone."""
    caps = None
    window = _window(caps)
    before = _states(window._profile_buttons)
    window._apply_capabilities(caps)
    assert _states(window._profile_buttons) == before


def test_a_broken_button_cannot_break_capability_application():
    """One widget refusing to configure must not abort the whole update."""
    caps = Capabilities(control_backend="wmi-bios", supports_thermal=False,
                        supports_charge=False)

    class Exploding(FakeButton):
        def configure(self, **options):
            raise RuntimeError("widget destroyed")

    window = _window(caps)
    window._profile_buttons[ThermalProfile.QUIET] = Exploding("Quiet")
    window._apply_capabilities(caps)  # must not raise

    assert window._profile_buttons[ThermalProfile.COOL].state == "disabled"


# ---------------------------------------------------------------------------
# The service layer must refuse an impossible write with a useful message
# ---------------------------------------------------------------------------
def test_service_refuses_profile_change_when_attribute_is_absent(monkeypatch):
    from xpshelper.service import make_service

    service = make_service("mock")
    caps = Capabilities(control_backend="wmi-bios", supports_thermal=False)
    monkeypatch.setattr(service, "capabilities", lambda refresh=False: caps)

    with pytest.raises(ValidationError) as excinfo:
        service.set_profile(ThermalProfile.QUIET)
    message = str(excinfo.value)
    assert "no thermal-profile setting" in message
    # It must not leak a raw firmware status at the user.
    assert "status" not in message.lower()


def test_service_refuses_charge_change_when_attribute_is_absent(monkeypatch):
    from xpshelper.service import make_service

    service = make_service("mock")
    caps = Capabilities(control_backend="wmi-bios", supports_charge=False)
    monkeypatch.setattr(service, "capabilities", lambda refresh=False: caps)

    with pytest.raises(ValidationError) as excinfo:
        service.set_charge(ChargeMode.STANDARD)
    assert "no battery charge configuration attribute" in str(excinfo.value)


def test_service_still_works_when_support_is_unknown(monkeypatch):
    """Fail-open must survive: None means unknown, not unsupported."""
    from xpshelper.service import make_service

    service = make_service("mock")
    caps = Capabilities(control_backend="mock", supports_thermal=None, supports_charge=None)
    monkeypatch.setattr(service, "capabilities", lambda refresh=False: caps)

    assert service.set_profile(ThermalProfile.QUIET) is ThermalProfile.QUIET
    assert service.set_charge(ChargeMode.STANDARD).mode is ChargeMode.STANDARD


def test_failed_profile_selection_is_reverted_in_the_ui():
    """A radio button moves on click, before the firmware agrees.

    Leaving it moved after a refusal makes the window claim a setting that never
    took effect -- which is what the screenshot showed.
    """
    import inspect

    source = inspect.getsource(HelperWindow._on_profile_selected)
    assert "_active_profile" in source
    assert "_profile_var.set(previous.value)" in source


# ---------------------------------------------------------------------------
# Dell ACPI-WMI thermal modes
#
# Added once the interface was reverse-engineered. The radios must reflect what the
# firmware reports and must not present a mode the firmware does not offer.
# ---------------------------------------------------------------------------
def test_the_unavailable_reason_survives_a_saved_profile(monkeypatch):
    """A saved profile must not overwrite the explanation for disabled buttons.

    Regression test for what the window actually showed on an XPS 13 7390 2-in-1:
    every Performance button was correctly disabled, but the hint underneath read
    "Fans run slower and CPU power is capped (~11 W on XPS 13)...". That is the
    *Quiet* profile description, loaded from settings by _sync_from_service. With
    nothing saying why, greyed-out buttons just look broken.

    Two methods write this one label, so the explanation has to win when the
    capability is known to be absent.
    """
    window = _window(None)
    window._profile_unavailable = False
    window._thermal_var = SimpleNamespace(get=lambda: "", set=lambda v: None)
    window._charge_var = SimpleNamespace(get=lambda: ChargeMode.STANDARD.value)
    window._charge_hint = FakeWidget(text='')
    window._update_hints = HelperWindow._update_hints.__get__(window)

    # A profile is selected, as it would be from a settings file.
    window._profile_var = SimpleNamespace(get=lambda: ThermalProfile.QUIET.value)

    caps = Capabilities(
        control_backend="wmi-bios",
        supports_thermal=False,
        supports_charge=False,
    )
    window._apply_capabilities(caps)
    reason = window._profile_hint.text
    assert "no thermal-profile setting" in reason, reason

    # Anything that refreshes the hints afterwards must not clobber it.
    window._update_hints()
    assert window._profile_hint.text == reason, window._profile_hint.text


def test_a_working_machine_still_shows_the_profile_description():
    """The explanation must not stick once thermal support is genuinely present."""
    from xpshelper.model import ThermalProfile as _TP

    window = _window(None)
    window._profile_unavailable = True          # left over from a disabled run
    window._thermal_var = SimpleNamespace(get=lambda: "", set=lambda v: None)
    window._charge_var = SimpleNamespace(get=lambda: ChargeMode.STANDARD.value)
    window._charge_hint = FakeWidget(text='')
    window._update_hints = HelperWindow._update_hints.__get__(window)
    window._profile_var = SimpleNamespace(get=lambda: _TP.QUIET.value)

    caps = Capabilities(
        control_backend="wmi-bios",
        supports_thermal=True,
        supports_charge=True,
        thermal_profiles=(_TP.QUIET,),
        charge_modes=(ChargeMode.STANDARD,),
    )
    window._apply_capabilities(caps)
    assert window._profile_unavailable is False
    window._update_hints()
    assert "silent" in window._profile_hint.text.lower()


def _thermal_window() -> SimpleNamespace:
    """A stand-in exposing only what the thermal handlers touch."""
    window = SimpleNamespace(
        _thermal_buttons={
            name: FakeButton(name) for name in THERMAL_MODE_ORDER
        },
        _thermal_hint=FakeWidget(text=""),
        _thermal_var=SimpleNamespace(_value="", set=lambda v: None),
    )
    window._enable_only = HelperWindow._enable_only.__get__(window)
    window._mark_selected = HelperWindow._mark_selected.__get__(window)
    window._set_thermal_hint = HelperWindow._set_thermal_hint.__get__(window)
    window._apply_thermal_result = HelperWindow._apply_thermal_result.__get__(window)
    return window


def _thermal_tables(mask=0x0F, current=1):
    from xpshelper import thermal

    return thermal.ThermalTables(
        error=None, supported_mask=mask, current_mode=current, fan_failure=0
    )


def test_every_thermal_mode_is_enabled_when_the_firmware_offers_them():
    window = _thermal_window()
    window._apply_thermal_result(_thermal_tables(mask=0x0F, current=1))
    assert all(b.state == "normal" for b in window._thermal_buttons.values())


def test_modes_the_firmware_does_not_offer_are_disabled():
    """This machine reported 0x0f, but a 0x03 machine must not show Quiet/Ultra."""
    window = _thermal_window()
    window._apply_thermal_result(_thermal_tables(mask=0x03, current=1))
    states = _states(window._thermal_buttons)
    assert states["Balanced"] == "normal"
    assert states["Cool"] == "normal"
    assert states["Quiet"] == "disabled"
    assert states["Ultra"] == "disabled"


def test_the_current_mode_is_selected_from_the_firmware_report():
    window = _thermal_window()
    selected = {}
    window._thermal_var = SimpleNamespace(set=lambda v: selected.__setitem__("v", v))
    window._apply_thermal_result(_thermal_tables(current=4))
    assert selected["v"] == "Quiet"


def test_a_report_with_no_modes_says_so_rather_than_showing_buttons():
    window = _thermal_window()
    window._apply_thermal_result(_thermal_tables(mask=0x00, current=0))
    assert all(b.state == "disabled" for b in window._thermal_buttons.values())
    assert "no thermal modes" in window._thermal_hint.text


def test_thermal_order_covers_every_recovered_mode():
    from xpshelper import thermal

    assert set(THERMAL_MODE_ORDER) == set(thermal.THERMAL_MODES)
