"""GUI tests for the display-free parts.

The widgets themselves cannot be exercised headlessly, so the logic the window
depends on -- text formatting, capability filtering, icon colours, and graceful
failure without a display -- is tested directly.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from xpshelper.backends.mock import MockBackend
from xpshelper.gui.app import (
    battery_text,
    charge_options,
    profile_options,
    status_rows,
)
from xpshelper.gui.icon import FALLBACK_COLOR, color_for, make_icon_image
from xpshelper.model import (
    BatteryStatus,
    Capabilities,
    ChargeConfig,
    ChargeMode,
    FanReading,
    TemperatureReading,
    ThermalProfile,
)
from xpshelper.service import ConfigStore, XpsHelper

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_importing_gui_package_does_not_pull_in_tkinter():
    """The CLI imports the GUI lazily; importing the package must stay cheap."""
    code = (
        "import sys, xpshelper.gui;"
        "assert 'tkinter' not in sys.modules, 'tkinter was imported eagerly'"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        env={**os.environ, "PYTHONPATH": "src"},
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def test_battery_text_full():
    battery = BatteryStatus(
        percent=76,
        plugged_in=False,
        charging=False,
        health_percent=91.3,
        minutes_remaining=214,
    )
    text = battery_text(battery)
    assert "76%" in text
    assert "on battery" in text
    assert "health 91.3%" in text
    assert "3h34m left" in text


def test_battery_text_charging():
    text = battery_text(BatteryStatus(percent=50, plugged_in=True, charging=True))
    assert "charging" in text


def test_battery_text_unavailable():
    assert battery_text(None) == "unavailable"


def test_status_rows_includes_battery_temperature_and_fan():
    from xpshelper.backends.base import collect_status

    backend = MockBackend()
    status = collect_status(backend, backend)
    rows = dict(status_rows(status))

    assert "76%" in rows["Battery"]
    assert "CPU Package" in rows["Temperature"]
    assert "2413 RPM" in rows["Fan"]
    assert "Adaptive" in rows["Charge setting"]


def test_status_rows_handles_missing_sensors():
    from xpshelper.model import SystemStatus

    rows = dict(status_rows(SystemStatus()))
    assert rows["Battery"] == "unavailable"
    assert rows["Temperature"] == "unavailable"
    assert "Fan" not in rows  # omitted rather than shown empty
    assert rows["Charge setting"] == "unavailable"


def test_status_rows_with_custom_charge():
    from xpshelper.model import SystemStatus

    status = SystemStatus(charge=ChargeConfig(mode=ChargeMode.CUSTOM, start=60, stop=80))
    rows = dict(status_rows(status))
    assert "60%" in rows["Charge setting"]


# ---------------------------------------------------------------------------
# Option filtering
# ---------------------------------------------------------------------------
def test_profile_options_uses_probe_result():
    caps = Capabilities(thermal_profiles=(ThermalProfile.OPTIMIZED, ThermalProfile.QUIET))
    assert profile_options(caps) == (ThermalProfile.OPTIMIZED, ThermalProfile.QUIET)


def test_profile_options_falls_back_to_all_when_probe_empty():
    assert profile_options(Capabilities()) == tuple(ThermalProfile)


def test_charge_options_falls_back_to_all_when_probe_empty():
    assert charge_options(Capabilities()) == tuple(ChargeMode)


# ---------------------------------------------------------------------------
# Icons
# ---------------------------------------------------------------------------
def test_color_for_each_profile_is_distinct():
    colors = {color_for(profile) for profile in ThermalProfile}
    assert len(colors) == len(ThermalProfile)
    assert FALLBACK_COLOR not in colors


def test_color_for_unknown_profile_uses_fallback():
    assert color_for(None) == FALLBACK_COLOR


def test_make_icon_image_returns_none_without_pillow(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("PIL"):
            raise ImportError("Pillow not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert make_icon_image(ThermalProfile.QUIET) is None


# ---------------------------------------------------------------------------
# Headless behaviour
# ---------------------------------------------------------------------------
@pytest.mark.skipif(sys.platform == "win32", reason="DISPLAY semantics differ on Windows")
def test_run_gui_without_display_fails_gracefully(tmp_path, monkeypatch, capsys):
    """A headless box must get an explanation, not a traceback."""
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    from xpshelper.gui.app import run_gui

    service = XpsHelper(
        control=MockBackend(),
        telemetry=MockBackend(),
        store=ConfigStore(tmp_path / "config.json"),
    )
    code = run_gui(service, use_tray=False)
    assert code == 1
    assert "Could not open the window" in capsys.readouterr().err
