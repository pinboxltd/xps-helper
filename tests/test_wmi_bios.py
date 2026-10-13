"""Tests for the native WMI-ACPI BIOS-attribute backend.

This is the primary control path on Windows and requires no Dell software, so it
gets thorough coverage of the parts that can be verified off-hardware: JSON
decoding, capability derivation from ``PossibleValues``, status-code
classification, and the security property that a BIOS password is never written
into the command text as a plain string literal.
"""

from __future__ import annotations

import base64
import json
import re
import sys

import pytest

from xpshelper.backends.wmi_bios import (
    WmiBiosBackend,
    build_script,
    current_value,
    encode_payload,
    find_attribute,
    normalise_attribute_list,
    parse_json_output,
    possible_values,
    status_to_exception,
)
from xpshelper import constants as C
from xpshelper.errors import (
    AttributeNotSupported,
    BiosPasswordRequired,
    CctkError,
)
from xpshelper.model import ChargeConfig, ChargeMode, ThermalProfile

PAYLOAD_RE = re.compile(r"FromBase64String\('([^']+)'\)")


class FakeWmiBios(WmiBiosBackend):
    """The backend refuses to run off Windows; force it on for these tests."""

    @property
    def available(self) -> bool:
        return True


class ScriptedRunner:
    """Returns canned PowerShell output and records what was asked."""

    def __init__(self, read_json: dict | None = None, write_json: dict | None = None):
        self.read_json = read_json or {}
        self.write_json = write_json or {"status": 0}
        self.scripts: list[str] = []

    def __call__(self, script: str, timeout: int):
        self.scripts.append(script)
        payload = json.loads(
            base64.b64decode(PAYLOAD_RE.search(script).group(1)).decode("utf-8")
        )
        # The write script carries "attribute"; the read script carries "attributes".
        if "attribute" in payload:
            return 0, json.dumps(self.write_json), ""
        return 0, json.dumps(self.read_json), ""

    def payloads(self) -> list[dict]:
        out = []
        for script in self.scripts:
            match = PAYLOAD_RE.search(script)
            out.append(json.loads(base64.b64decode(match.group(1)).decode("utf-8")))
        return out


def attribute(name: str, current: str | None, values=None, cls="EnumerationAttribute"):
    entry = {"AttributeName": name, "AttributeClass": cls}
    if current is not None:
        entry["CurrentValue"] = current
    if values is not None:
        entry["PossibleValues"] = values
    return entry


# ---------------------------------------------------------------------------
# Payload encoding / script building
# ---------------------------------------------------------------------------
def test_payload_is_base64_and_not_plaintext():
    """A BIOS password must never appear as a PowerShell string literal."""
    script = build_script("Write-Output 1", {"password": "p@ss'`$(whoami)"})
    assert "p@ss" not in script
    assert "whoami" not in script
    encoded = PAYLOAD_RE.search(script).group(1)
    decoded = json.loads(base64.b64decode(encoded).decode("utf-8"))
    assert decoded["password"] == "p@ss'`$(whoami)"


def test_encode_payload_round_trip():
    payload = {"attribute": "ThermalManagement", "value": "Quiet"}
    assert json.loads(base64.b64decode(encode_payload(payload))) == payload


# ---------------------------------------------------------------------------
# JSON decoding
# ---------------------------------------------------------------------------
def test_parse_json_output_plain():
    assert parse_json_output('{"interface_found": true}') == {"interface_found": True}


def test_parse_json_output_with_leading_noise():
    """PowerShell can print warnings before the JSON document."""
    noisy = 'WARNING: something\n{"interface_found": true}\n'
    assert parse_json_output(noisy) == {"interface_found": True}


def test_parse_json_output_array_wrapped():
    assert parse_json_output('[{"interface_found": true}]') == {"interface_found": True}


@pytest.mark.parametrize("payload", ["", "   ", "not json", "null", "[]", "[1,2]"])
def test_parse_json_output_rejects_junk(payload):
    assert parse_json_output(payload) == {}


# ---------------------------------------------------------------------------
# Attribute helpers
# ---------------------------------------------------------------------------
def test_normalise_attribute_list_handles_dict_and_list():
    assert normalise_attribute_list(None) == []
    assert normalise_attribute_list({"a": 1}) == [{"a": 1}]
    assert normalise_attribute_list([{"a": 1}, "junk"]) == [{"a": 1}]


@pytest.mark.parametrize(
    "raw,expected",
    [
        (["Optimized", "Quiet", "Cool", "UltraPerformance"],
         ["Optimized", "Quiet", "Cool", "UltraPerformance"]),
        ("Optimized, Quiet, Cool, UltraPerformance",
         ["Optimized", "Quiet", "Cool", "UltraPerformance"]),
        ("Optimized;Quiet;Cool", ["Optimized", "Quiet", "Cool"]),
        ("", []),
        (None, []),
    ],
)
def test_possible_values(raw, expected):
    assert possible_values({"PossibleValues": raw}) == expected


def test_possible_values_falls_back_to_description_property():
    entry = {"PossibleValuesDescription": "standard, express, primacuse"}
    assert possible_values(entry) == ["standard", "express", "primacuse"]


def test_possible_values_missing_returns_empty():
    assert possible_values({"AttributeName": "X"}) == []


def test_current_value():
    assert current_value({"CurrentValue": " Quiet "}) == "Quiet"
    assert current_value({"CurrentValue": ""}) is None
    assert current_value({}) is None


def test_find_attribute_is_case_insensitive():
    entries = [attribute("thermalmanagement", "Quiet")]
    assert find_attribute(entries, ("ThermalManagement",)) is entries[0]
    assert find_attribute(entries, ("Nope",)) is None


# ---------------------------------------------------------------------------
# Status code classification
# ---------------------------------------------------------------------------
def test_status_ok_returns_none():
    assert status_to_exception(0, attribute="A", value="B") is None


def test_status_access_denied_maps_to_password_required():
    exc = status_to_exception(3, attribute="ThermalManagement", value="Quiet")
    assert isinstance(exc, BiosPasswordRequired)


def test_status_not_supported_maps_to_unsupported():
    exc = status_to_exception(4, attribute="ThermalManagement", value="Quiet")
    assert isinstance(exc, AttributeNotSupported)
    assert "does not support" in str(exc)


def test_status_invalid_parameter_maps_to_unsupported():
    exc = status_to_exception(2, attribute="ThermalManagement", value="Bogus")
    assert isinstance(exc, AttributeNotSupported)


@pytest.mark.parametrize("status", [1, 5, 6, 99])
def test_other_statuses_map_to_generic_error(status):
    exc = status_to_exception(status, attribute="A", value="B")
    assert isinstance(exc, CctkError)
    assert exc.exit_code == status


def test_missing_status_is_an_error():
    assert isinstance(status_to_exception(None, attribute="A", value="B"), CctkError)


# ---------------------------------------------------------------------------
# Backend behaviour with canned BIOS responses
# ---------------------------------------------------------------------------
def test_backend_unavailable_off_windows():
    backend = WmiBiosBackend()
    if sys.platform == "win32":
        pytest.skip("this assertion is about non-Windows platforms")
    assert backend.available is False
    caps = backend.probe()
    assert caps.thermal_profiles == ()
    assert any("only available on Windows" in note for note in caps.notes)


def test_probe_reports_missing_interface_and_explains():
    runner = ScriptedRunner(read_json={"interface_found": False, "errors": ["not found"]})
    backend = FakeWmiBios(runner=runner)
    caps = backend.probe()

    assert caps.thermal_profiles == ()
    assert any("did not expose BIOSAttributeInterface" in note for note in caps.notes)
    assert any("not found" in note for note in caps.notes)


def test_probe_derives_capabilities_from_possible_values():
    runner = ScriptedRunner(
        read_json={
            "interface_found": True,
            "admin_password_set": False,
            "attributes": [
                attribute("ThermalManagement", "Quiet",
                          ["optimized", "quiet", "cool", "ultraperformance"]),
                attribute("PrimaryBattChargeCfg", "PrimAcUse",
                          ["standard", "express", "primacuse", "adaptive", "custom"]),
            ],
        }
    )
    caps = FakeWmiBios(runner=runner).probe()

    assert caps.thermal_profiles == (
        ThermalProfile.OPTIMIZED,
        ThermalProfile.QUIET,
        ThermalProfile.COOL,
        ThermalProfile.ULTRA_PERFORMANCE,
    )
    assert caps.supports_custom_thresholds
    assert caps.supports_advanced_schedule is False
    assert any("Native BIOS attributes available" in note for note in caps.notes)


def test_probe_fails_open_when_possible_values_missing():
    """Never hide a feature just because the BIOS did not report its value list."""
    runner = ScriptedRunner(
        read_json={
            "interface_found": True,
            "attributes": [attribute("ThermalManagement", "Quiet")],
        }
    )
    caps = FakeWmiBios(runner=runner).probe()

    assert caps.thermal_profiles == tuple(ThermalProfile)
    assert any("no PossibleValues" in note for note in caps.notes)


def test_probe_notes_when_admin_password_is_set():
    runner = ScriptedRunner(
        read_json={
            "interface_found": True,
            "admin_password_set": True,
            "attributes": [attribute("ThermalManagement", "Quiet", ["quiet"])],
        }
    )
    caps = FakeWmiBios(runner=runner).probe()
    assert any("administrator password is set" in note for note in caps.notes)


def test_read_profile_and_charge_config():
    runner = ScriptedRunner(
        read_json={
            "interface_found": True,
            "attributes": [
                attribute("ThermalManagement", "UltraPerformance"),
                attribute("PrimaryBattChargeCfg", "Custom:60-80"),
            ],
        }
    )
    backend = FakeWmiBios(runner=runner)
    assert backend.read_profile() is ThermalProfile.ULTRA_PERFORMANCE
    assert backend.read_charge_config() == ChargeConfig(
        mode=ChargeMode.CUSTOM, start=60, stop=80
    )


def test_read_profile_returns_none_when_attribute_absent():
    runner = ScriptedRunner(read_json={"interface_found": True, "attributes": []})
    assert FakeWmiBios(runner=runner).read_profile() is None


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------
def test_set_profile_sends_exact_token():
    runner = ScriptedRunner(write_json={"status": 0})
    FakeWmiBios(runner=runner).set_profile(ThermalProfile.QUIET)

    write_payload = [p for p in runner.payloads() if "attribute" in p][-1]
    assert write_payload["attribute"] == "ThermalManagement"
    assert write_payload["value"] == "Quiet"


def test_set_charge_sends_custom_token():
    runner = ScriptedRunner(
        read_json={
            "interface_found": True,
            "attributes": [attribute("PrimaryBattChargeCfg", "Adaptive", ["custom"])],
        }
    )
    FakeWmiBios(runner=runner).set_charge_config(
        ChargeConfig(mode=ChargeMode.CUSTOM, start=60, stop=80)
    )
    write_payload = [p for p in runner.payloads() if "attribute" in p][-1]
    assert write_payload["attribute"] == "PrimaryBattChargeCfg"
    assert write_payload["value"] == "Custom:60-80"


def test_set_profile_raises_on_access_denied():
    runner = ScriptedRunner(write_json={"status": 3})
    with pytest.raises(BiosPasswordRequired):
        FakeWmiBios(runner=runner).set_profile(ThermalProfile.QUIET)


def test_set_profile_raises_on_not_supported():
    runner = ScriptedRunner(write_json={"status": 4})
    with pytest.raises(AttributeNotSupported):
        FakeWmiBios(runner=runner).set_profile(ThermalProfile.QUIET)


def test_set_profile_raises_generic_error_on_failure():
    runner = ScriptedRunner(write_json={"status": 1})
    with pytest.raises(CctkError):
        FakeWmiBios(runner=runner).set_profile(ThermalProfile.QUIET)


def test_bios_password_is_sent_only_when_configured():
    runner = ScriptedRunner(write_json={"status": 0})
    backend = FakeWmiBios(runner=runner, bios_password="secret123")
    backend.set_profile(ThermalProfile.QUIET)
    write_payload = [p for p in runner.payloads() if "attribute" in p][-1]
    assert write_payload["password"] == "secret123"

    runner2 = ScriptedRunner(write_json={"status": 0})
    FakeWmiBios(runner=runner2).set_profile(ThermalProfile.QUIET)
    assert [p for p in runner2.payloads() if "attribute" in p][-1]["password"] is None


def test_interface_constants_match_the_documented_dell_interface():
    assert C.WMI_BIOS_NAMESPACE == r"root\dcim\sysman\biosattributes"
    assert C.WMI_BIOS_INTERFACE_CLASS == "BIOSAttributeInterface"
    assert C.WMI_BIOS_INTERFACE_METHOD == "SetAttribute"
    assert C.WMI_BIOS_STATUS_TEXT[0] == "Success"
    assert C.WMI_BIOS_STATUS_TEXT[3] == "Access denied"
    assert C.WMI_BIOS_STATUS_TEXT[4] == "Not supported"


# ---------------------------------------------------------------------------
# Dual-provider support. Regression guard for a real field failure: querying
# only the CIM provider reported "the BIOS did not expose BIOSAttributeInterface"
# on a machine where the interface exists but is reachable only over the legacy
# DCOM/WMI provider (which is what Dell's own sample scripts use).
# ---------------------------------------------------------------------------
def test_read_script_tries_cim_then_legacy_wmi():
    from xpshelper.backends.wmi_bios import READ_SCRIPT

    assert "Get-CimInstance" in READ_SCRIPT
    assert "Get-WmiObject" in READ_SCRIPT
    # The provider loop must try CIM first, then the legacy DCOM provider.
    assert "foreach ($api in @('cim', 'wmi'))" in READ_SCRIPT
    assert READ_SCRIPT.index("Get-CimInstance -Namespace") < READ_SCRIPT.index(
        "Get-WmiObject -Namespace"
    )


def test_write_script_tries_both_invoke_methods():
    from xpshelper.backends.wmi_bios import WRITE_SCRIPT

    assert "Invoke-CimMethod" in WRITE_SCRIPT
    assert "Invoke-WmiMethod" in WRITE_SCRIPT


def test_probe_records_which_provider_answered():
    runner = ScriptedRunner(
        read_json={
            "interface_found": True,
            "api": "wmi",
            "attributes": [attribute("ThermalManagement", "Quiet", ["quiet"])],
        }
    )
    backend = FakeWmiBios(runner=runner)
    assert backend.probe().thermal_profiles == (ThermalProfile.QUIET,)
    assert backend.active_provider() == "wmi"
    assert any("WMI provider" in note for note in backend.probe().notes)


def test_probe_surfaces_provider_errors_when_interface_missing():
    runner = ScriptedRunner(
        read_json={
            "interface_found": False,
            "errors": [
                "cim:BIOSAttributeInterface: Not supported",
                "wmi:BIOSAttributeInterface: Invalid namespace",
            ],
        }
    )
    caps = FakeWmiBios(runner=runner).probe()

    notes = " ".join(caps.notes)
    assert "cim:BIOSAttributeInterface" in notes
    assert "wmi:BIOSAttributeInterface" in notes
    # And it should point the user at the deep diagnostic.
    assert "diagnose-wmi" in notes


def test_probe_handles_errors_field_as_plain_string():
    """PowerShell emits a bare string when an array has exactly one element."""
    runner = ScriptedRunner(
        read_json={"interface_found": False, "errors": "cim: something went wrong"}
    )
    caps = FakeWmiBios(runner=runner).probe()
    assert any("something went wrong" in note for note in caps.notes)


def test_refresh_records_last_error_from_provider_errors():
    runner = ScriptedRunner(
        read_json={"interface_found": False, "errors": ["wmi: Access is denied"]}
    )
    backend = FakeWmiBios(runner=runner)
    backend.refresh()
    assert "Access is denied" in (backend.last_error or "")


def test_runner_is_called_with_hidden_window_args_on_windows(monkeypatch):
    """The GUI must never flash a PowerShell console window."""
    import sys

    from xpshelper.backends import wmi_bios

    monkeypatch.setattr(sys, "platform", "win32")
    captured = {}

    class FakeProc:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr(wmi_bios.subprocess, "run", fake_run)
    runner = wmi_bios.default_powershell_runner("Write-Output 1", 5)

    assert runner == (0, "{}", "")
    assert captured["kwargs"].get("creationflags") == 0x08000000
    # STARTUPINFO is a Windows-only subprocess attribute; on other platforms the
    # creation flag alone is what suppresses the console.
    import subprocess

    if hasattr(subprocess, "STARTUPINFO"):
        assert "startupinfo" in captured["kwargs"]
