"""Tests for the WMI diagnostic report and the subprocess window suppression."""

from __future__ import annotations

import json
import sys

import pytest

from xpshelper.backends.wmi_diagnose import (
    DIAGNOSE_SCRIPT,
    render,
    run_diagnosis,
    to_json,
)
from xpshelper.procutil import CREATE_NO_WINDOW, hidden_process_kwargs, powershell_argv


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------
def test_hidden_process_kwargs_is_empty_off_windows():
    if sys.platform == "win32":
        pytest.skip("this assertion is about non-Windows platforms")
    assert hidden_process_kwargs() == {}


def test_hidden_process_kwargs_suppresses_console_on_windows(monkeypatch):
    """The GUI has no console, so every child process must be hidden.

    Without this, Windows allocates a console window for each PowerShell call and
    the app appears to flash black boxes at the user on every refresh.
    """
    monkeypatch.setattr(sys, "platform", "win32")
    kwargs = hidden_process_kwargs()
    assert kwargs.get("creationflags") == CREATE_NO_WINDOW
    assert CREATE_NO_WINDOW == 0x08000000


def test_powershell_argv_shape():
    argv = powershell_argv("Write-Output 1")
    assert argv[0] == "powershell"
    assert "-NoProfile" in argv
    assert "-NonInteractive" in argv
    assert argv[-1] == "Write-Output 1"


# ---------------------------------------------------------------------------
# The diagnostic script must probe both providers
# ---------------------------------------------------------------------------
def test_diagnose_script_probes_both_wmi_providers():
    """Regression guard for the original bug.

    The first version queried only `Get-CimInstance`, so machines where the Dell
    class is reachable only over the legacy DCOM provider were wrongly reported
    as having no BIOS interface at all.
    """
    assert "Get-CimInstance" in DIAGNOSE_SCRIPT
    assert "Get-WmiObject" in DIAGNOSE_SCRIPT


def test_diagnose_script_checks_for_acpi_wmi_devices():
    """No PNP0C14 device means the firmware offers no WMI mapping at all."""
    assert "PNP0C14" in DIAGNOSE_SCRIPT


def test_diagnose_script_reports_key_facts():
    for needle in ("elevated", "namespaces", "probes", "target_attributes"):
        assert needle in DIAGNOSE_SCRIPT


# ---------------------------------------------------------------------------
# run_diagnosis
# ---------------------------------------------------------------------------
def sample_diagnosis(**overrides) -> dict:
    data = {
        "powershell": "5.1.22621.1",
        "elevated": True,
        "acpi_wmi_devices": ["ACPI\\PNP0C14\\0_0 Dell WMI"],
        "namespaces": ["root\\dcim", "root\\dcim\\sysman\\biosattributes"],
        "probes": [
            {
                "namespace": "root\\dcim\\sysman\\biosattributes",
                "class": "BIOSAttributeInterface",
                "cim": None,
                "cim_error": "Not supported",
                "wmi": "ok",
                "wmi_error": None,
                "count": 1,
                "attributes_sampled": [],
            }
        ],
        "target_attributes": [
            {
                "provider": "wmi",
                "class": "EnumerationAttribute",
                "AttributeName": "ThermalManagement",
                "CurrentValue": "Optimized",
                "PossibleValues": "optimized,quiet,cool,ultraperformance",
            }
        ],
        "wmi_classes_of_interest": ["Dell_Something"],
        "system": {"vendor": "Dell Inc.", "model": "XPS 13 7390 2-in-1", "bios": "1.20.0", "is_dell": True},
    }
    data.update(overrides)
    return data


def test_run_diagnosis_decodes_runner_output():
    payload = json.dumps(sample_diagnosis())

    def runner(argv, timeout):
        assert argv[0] == "powershell"
        return 0, payload, ""

    result = run_diagnosis(runner=runner)
    assert result["elevated"] is True
    assert result["probes"][0]["wmi"] == "ok"


def test_run_diagnosis_captures_stderr_when_output_is_unusable():
    def runner(argv, timeout):
        return 1, "not json", "Access is denied."

    result = run_diagnosis(runner=runner)
    assert "error" in result
    assert "Access is denied" in result["stderr"]


def test_run_diagnosis_off_windows_without_runner():
    if sys.platform == "win32":
        pytest.skip("this assertion is about non-Windows platforms")
    result = run_diagnosis()
    assert "only meaningful on Windows" in result["error"]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def test_render_shows_both_providers_per_candidate():
    text = render(sample_diagnosis())
    assert "CIM  : unavailable" in text
    assert "WMI  : OK" in text
    assert "Not supported" in text


def test_render_reports_absence_of_acpi_wmi_devices_explicitly():
    text = render(sample_diagnosis(acpi_wmi_devices=[]))
    assert "NONE" in text
    assert "cannot work on this" in text  # render() wraps long lines


def test_render_lists_target_attribute_values():
    text = render(sample_diagnosis())
    assert "ThermalManagement = Optimized" in text
    assert "optimized,quiet,cool,ultraperformance" in text


def test_render_when_nothing_found():
    text = render(
        sample_diagnosis(probes=[], target_attributes=[], namespaces=[], acpi_wmi_devices=[])
    )
    assert "NONE of ThermalManagement" in text


def test_render_includes_error_block():
    text = render({"error": "Could not decode", "stderr": "boom"})
    assert "ERROR: Could not decode" in text
    assert "boom" in text


def test_to_json_is_serialisable():
    payload = json.loads(to_json(sample_diagnosis()))
    assert payload["system"]["model"] == "XPS 13 7390 2-in-1"
