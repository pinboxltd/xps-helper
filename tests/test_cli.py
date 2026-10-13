"""CLI tests: argument handling, exit codes, and JSON output."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from xpshelper import constants as C
from xpshelper.cli import EXIT_ERROR, EXIT_OK, EXIT_USAGE, main


@pytest.fixture
def cfg(tmp_path: Path) -> str:
    return str(tmp_path / "config.json")


def run(*args: str) -> int:
    return main(list(args))


# ---------------------------------------------------------------------------
# Argument handling
# ---------------------------------------------------------------------------
def test_global_flags_before_subcommand(cfg, capsys):
    """Regression test for an argparse aliasing bug.

    ``set_defaults()`` mutates the shared Action objects created by
    ``parents=[...]``, so ``--backend mock`` given before the subcommand used to
    be silently reset to "auto".
    """
    assert run("--backend", "mock", "--config", cfg, "capabilities") == EXIT_OK
    assert "control backend   : mock" in capsys.readouterr().out


def test_global_flags_after_subcommand(cfg, capsys):
    assert run("capabilities", "--backend", "mock", "--config", cfg) == EXIT_OK
    assert "control backend   : mock" in capsys.readouterr().out


def test_missing_command_prints_help(cfg, capsys):
    assert run("--config", cfg) == EXIT_USAGE
    assert "usage:" in capsys.readouterr().out.lower()


def test_version_flag_exits_zero(capsys):
    with pytest.raises(SystemExit) as excinfo:
        run("--version")
    assert excinfo.value.code == 0
    assert C.APP_VERSION in capsys.readouterr().out


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------
def test_status_reports_mock_state(cfg, capsys):
    assert run("--backend", "mock", "--config", cfg, "status") == EXIT_OK
    out = capsys.readouterr().out
    assert "Thermal profile : Optimized" in out
    assert "Battery" in out
    assert "CPU Package" in out


def test_status_json_is_parseable(cfg, capsys):
    assert run("--backend", "mock", "--config", cfg, "status", "--json") == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["profile"] == "Optimized"
    assert payload["battery"]["percent"] == 76
    assert payload["capabilities"]["control_backend"] == "mock"
    assert payload["temperatures"]


# ---------------------------------------------------------------------------
# profile
# ---------------------------------------------------------------------------
def test_profile_list(cfg, capsys):
    assert run("--backend", "mock", "--config", cfg, "profile") == EXIT_OK
    out = capsys.readouterr().out
    assert "UltraPerformance" in out
    assert "* = active" in out


def test_profile_set_and_persist(cfg, capsys):
    assert run("--backend", "mock", "--config", cfg, "profile", "quiet") == EXIT_OK
    assert "Quiet" in capsys.readouterr().out

    # A fresh process must see the change (mock seeded from saved settings).
    assert run("--backend", "mock", "--config", cfg, "status", "--json") == EXIT_OK
    assert json.loads(capsys.readouterr().out)["profile"] == "Quiet"


def test_profile_accepts_friendly_alias(cfg, capsys):
    assert run("--backend", "mock", "--config", cfg, "profile", "ultra-performance") == EXIT_OK
    capsys.readouterr()  # discard the human-readable output
    assert run("--backend", "mock", "--config", cfg, "status", "--json") == EXIT_OK
    assert json.loads(capsys.readouterr().out)["profile"] == "UltraPerformance"


def test_profile_unknown_name_fails(cfg, capsys):
    assert run("--backend", "mock", "--config", cfg, "profile", "ludicrous") == EXIT_ERROR
    assert "Unknown thermal profile" in capsys.readouterr().err


def test_profile_json_output(cfg, capsys):
    assert run("--backend", "mock", "--config", cfg, "profile", "cool", "--json") == EXIT_OK
    assert json.loads(capsys.readouterr().out)["applied"] == "Cool"


# ---------------------------------------------------------------------------
# charge
# ---------------------------------------------------------------------------
def test_charge_custom_thresholds(cfg, capsys):
    assert (
        run("--backend", "mock", "--config", cfg, "charge", "custom", "--start", "60", "--stop", "80")
        == EXIT_OK
    )
    assert "60%" in capsys.readouterr().out

    assert run("--backend", "mock", "--config", cfg, "status", "--json") == EXIT_OK
    charge = json.loads(capsys.readouterr().out)["charge"]
    assert charge == {"mode": "Custom", "start": 60, "stop": 80, "token": "Custom:60-80"}


def test_charge_primarily_ac_alias(cfg, capsys):
    assert run("--backend", "mock", "--config", cfg, "charge", "primarily-ac") == EXIT_OK
    assert "Primarily AC Use" in capsys.readouterr().out


def test_charge_custom_without_thresholds_fails(cfg, capsys):
    assert run("--backend", "mock", "--config", cfg, "charge", "custom") == EXIT_ERROR
    assert "needs both" in capsys.readouterr().err


def test_charge_out_of_range_threshold_fails(cfg, capsys):
    code = run(
        "--backend", "mock", "--config", cfg, "charge", "custom", "--start", "40", "--stop", "80"
    )
    assert code == EXIT_ERROR
    assert "50-95" in capsys.readouterr().err


def test_charge_list(cfg, capsys):
    assert run("--backend", "mock", "--config", cfg, "charge") == EXIT_OK
    out = capsys.readouterr().out
    assert "PrimAcUse" in out
    assert "Express Charge" in out


# ---------------------------------------------------------------------------
# doctor / capabilities
# ---------------------------------------------------------------------------
def test_doctor_on_mock_succeeds(cfg, capsys):
    assert run("--backend", "mock", "--config", cfg, "doctor") == EXIT_OK
    out = capsys.readouterr().out
    assert "doctor report" in out
    assert "Verdict" in out


def test_doctor_json_has_checks(cfg, capsys):
    assert run("--backend", "mock", "--config", cfg, "doctor", "--json") == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["checks"]
    assert payload["worst_status"] in {"ok", "warn", "info"}


def test_doctor_without_control_backend_fails(cfg, capsys):
    assert run("--backend", "null", "--config", cfg, "doctor") == EXIT_ERROR
    assert "Verdict" in capsys.readouterr().out


def test_capabilities_json(cfg, capsys):
    assert run("--backend", "mock", "--config", cfg, "capabilities", "--json") == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["thermal_profiles"] == [
        "Optimized",
        "Quiet",
        "Cool",
        "UltraPerformance",
    ]
    assert payload["supports_custom_thresholds"] is True


# ---------------------------------------------------------------------------
# settings / apply
# ---------------------------------------------------------------------------
def test_settings_update_and_show(cfg, capsys):
    assert (
        run("--backend", "mock", "--config", cfg, "settings", "--refresh-seconds", "9")
        == EXIT_OK
    )
    assert "refresh_seconds          : 9" in capsys.readouterr().out


def test_settings_boolean_flag_pair(cfg, capsys):
    assert (
        run(
            "--backend", "mock", "--config", cfg,
            "settings", "--apply-profile-on-startup",
        )
        == EXIT_OK
    )
    capsys.readouterr()  # discard the human-readable output
    payload = json.loads(
        run_json(capsys, "--backend", "mock", "--config", cfg, "settings", "--json")
    )
    assert payload["apply_profile_on_startup"] is True


def test_apply_reports_nothing_to_do(cfg, capsys):
    assert run("--backend", "mock", "--config", cfg, "apply") == EXIT_OK
    assert "Nothing to apply" in capsys.readouterr().out


def test_apply_uses_saved_settings(cfg, capsys):
    run("--backend", "mock", "--config", cfg, "profile", "quiet")
    run("--backend", "mock", "--config", cfg, "settings", "--apply-profile-on-startup")
    capsys.readouterr()

    assert run("--backend", "mock", "--config", cfg, "apply") == EXIT_OK
    assert "Applied thermal profile: Quiet" in capsys.readouterr().out


def run_json(capsys, *args: str) -> str:
    assert main(list(args)) == EXIT_OK
    return capsys.readouterr().out
