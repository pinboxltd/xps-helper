"""Doctor report tests.

`doctor` exists so that a hardware problem reports itself. These tests pin the
behaviour that matters: it must include Dell's raw output when parsing fails,
and it must fail loudly when control is genuinely unavailable.
"""

from __future__ import annotations

import json

import pytest

from conftest import CHARGE_HELP_FULL, THERMAL_HELP_PARTIAL, build_cctk
from xpshelper.backends.mock import MockBackend
from xpshelper.doctor import FAIL, OK, WARN, run_doctor
from xpshelper.service import ConfigStore, XpsHelper


def make_service(tmp_path, control, telemetry=None) -> XpsHelper:
    return XpsHelper(
        control=control,
        telemetry=telemetry or MockBackend(),
        store=ConfigStore(tmp_path / "config.json"),
    )


def check_named(report, needle: str):
    for check in report.checks:
        if needle in check.name:
            return check
    raise AssertionError(f"no check named like {needle!r}; got {[c.name for c in report.checks]}")


def test_doctor_on_mock_backend(tmp_path):
    service = make_service(tmp_path, MockBackend())
    report = run_doctor(service)

    names = [check.name for check in report.checks]
    assert "Platform" in names
    assert "Battery" in names
    assert "Temperatures" in names
    assert "Verdict" in names
    assert not report.has_failures
    assert "doctor report" in report.render()


def test_doctor_reports_supported_thermal_values(tmp_path):
    exe_dir = tmp_path
    control, _ = build_cctk(
        exe_dir,
        {
            "-H --ThermalManagement": (0, THERMAL_HELP_PARTIAL),
            "-H --PrimaryBattChargeCfg": (0, CHARGE_HELP_FULL),
            "-H --AdvBatteryChargeCfg": (0, ""),
            "--Version": (0, "Dell Command | Configure 4.2.1"),
            "--ThermalManagement": (0, "ThermalManagement=Quiet"),
            "--PrimaryBattChargeCfg": (0, "PrimaryBattChargeCfg=PrimAcUse"),
        },
    )
    report = run_doctor(make_service(tmp_path, control))

    thermal = check_named(report, "Thermal profiles")
    assert thermal.status == OK
    assert "optimized" in thermal.detail
    assert "cool" in thermal.detail  # listed, but as reported-unsupported
    assert "Reported unsupported: cool" in thermal.detail

    profile = check_named(report, "Current thermal profile")
    assert profile.status == OK
    assert "Quiet" in profile.detail

    assert not report.has_failures


def test_doctor_includes_raw_output_when_parsing_fails(tmp_path):
    """If our parser does not understand Dell's text, the text must be shown.

    Without this, a parsing bug would look like 'your laptop does not support
    this', and there would be nothing to debug from.
    """
    control, _ = build_cctk(
        tmp_path,
        {
            "-H --ThermalManagement": (0, "Usage: cctk --ThermalManagement=<value>"),
            "-H --PrimaryBattChargeCfg": (0, "Usage: cctk --PrimaryBattChargeCfg=<value>"),
            "-H --AdvBatteryChargeCfg": (0, ""),
            "--Version": (0, "4.2.1"),
        },
    )
    report = run_doctor(make_service(tmp_path, control))

    thermal = check_named(report, "Thermal profiles")
    assert thermal.status == WARN
    assert "Could not parse" in thermal.detail
    assert "cctk -H --ThermalManagement" in report.raw_sections
    assert "Usage: cctk --ThermalManagement" in report.raw_sections["cctk -H --ThermalManagement"]

    # The raw text must also survive JSON serialisation for bug reports.
    assert json.loads(json.dumps(report.to_dict()))["raw_sections"]


def test_doctor_flags_missing_cctk_as_failure(tmp_path):
    """cctk is now optional, but an explicitly requested cctk that is missing fails."""
    control, _ = build_cctk(tmp_path, {})
    control.path = None  # simulate Dell Command | Configure not installed

    report = run_doctor(make_service(tmp_path, control))

    cctk_check = check_named(report, "Dell Command | Configure")
    assert cctk_check.status == FAIL
    assert "was not found" in cctk_check.detail
    # The message must not imply Dell software is required.
    assert "optional" in cctk_check.detail
    assert report.has_failures


def test_doctor_notes_missing_telemetry(tmp_path):
    telemetry = MockBackend(battery=False, temperatures=False, fans=False)
    service = make_service(tmp_path, MockBackend(), telemetry)
    report = run_doctor(service)

    assert check_named(report, "Battery").status == WARN
    assert check_named(report, "Temperatures").status == WARN
    assert check_named(report, "Fan speeds").status == WARN


def test_doctor_report_is_json_serialisable(tmp_path):
    report = run_doctor(make_service(tmp_path, MockBackend()))
    payload = json.loads(json.dumps(report.to_dict()))
    assert payload["worst_status"] in {"ok", "warn", "fail"}
    assert all({"name", "status", "detail"} <= set(c) for c in payload["checks"])


def test_render_marks_each_status(tmp_path):
    control, _ = build_cctk(tmp_path, {})
    control.path = None
    rendered = run_doctor(make_service(tmp_path, control)).render()
    assert "[fail]" in rendered


@pytest.mark.parametrize("needle", ["Platform"])
def test_doctor_reports_platform_line(tmp_path, needle):
    report = run_doctor(make_service(tmp_path, MockBackend()))
    assert needle in check_named(report, needle).name
