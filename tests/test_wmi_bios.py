"""Tests for the native WMI-ACPI BIOS-attribute backend.

This is the primary control path on Windows and requires no Dell software, so it
gets thorough coverage of the parts that can be verified off-hardware: JSON
decoding, capability derivation from the firmware's ``PossibleValue`` array,
status-code classification, and the security property that a BIOS password is
never written into the command text as a plain string literal.

The canned payload shapes here are not invented: they mirror what an actual
XPS 13 7390 2-in-1 running BIOS 1.35.0 returns, including the property names and
the fact that ``ThermalManagement`` is absent on that model.
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
    """Returns canned PowerShell output and records what was asked.

    The backend sends two different scripts: a read (payload key ``attributes`` /
    ``value_attributes``) and a write (payload key ``writes``). The canned reply
    is filtered to the attributes that were actually asked about, because the
    real firmware only returns the rows you query for -- and the custom-charge
    thresholds are fetched with a *separate* read.
    """

    def __init__(
        self,
        read_json: dict | None = None,
        write_json: dict | None = None,
        write_statuses: dict | None = None,
    ):
        self.read_json = read_json or {}
        self.write_json = write_json or {"status": 0}
        self.write_statuses = write_statuses
        self.scripts: list[str] = []
        self.payloads_seen: list[dict] = []

    def __call__(self, script: str, timeout: int):
        self.scripts.append(script)
        payload = json.loads(
            base64.b64decode(PAYLOAD_RE.search(script).group(1)).decode("utf-8")
        )
        self.payloads_seen.append(payload)

        if "writes" in payload:
            entries = []
            for item in payload["writes"]:
                status = 0
                if self.write_statuses is not None:
                    status = self.write_statuses.get(item["attribute"], 0)
                else:
                    status = self.write_json.get("status", 0)
                entries.append({"attribute": item["attribute"], "status": status})
            if "results" in self.write_json:
                return 0, json.dumps(self.write_json), ""
            response = {
                "api": self.write_json.get("api", "cim"),
                "error": self.write_json.get("error"),
                "results": entries,
            }
            return 0, json.dumps(response), ""

        want = set(payload.get("attributes") or []) | set(payload.get("value_attributes") or [])
        rows = [
            row
            for row in (self.read_json.get("attributes") or [])
            if not want or row.get("AttributeName") in want
        ]
        response = dict(self.read_json)
        response["attributes"] = rows
        return 0, json.dumps(response), ""

    def payloads(self) -> list[dict]:
        return list(self.payloads_seen)

    def write_payloads(self) -> list[dict]:
        return [p for p in self.payloads_seen if "writes" in p]

    def read_payloads(self) -> list[dict]:
        return [p for p in self.payloads_seen if "attributes" in p]

    def writes_asked(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for payload in self.write_payloads():
            for item in payload["writes"]:
                out.append((item["attribute"], item["value"]))
        return out


def attribute(name: str, current: str | None, values=None, cls="EnumerationAttribute"):
    """Build one firmware attribute row.

    The property is ``PossibleValue`` (singular) -- that is what the Dell
    WMI-ACPI classes actually declare, confirmed against the class schema on a
    real XPS 13 7390 2-in-1.
    """
    entry = {"AttributeName": name, "AttributeClass": cls}
    if current is not None:
        entry["CurrentValue"] = current
    if values is not None:
        entry["PossibleValue"] = values
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
    assert possible_values({"PossibleValue": raw}) == expected


def test_possible_values_accepts_the_plural_spelling_too():
    """Older revisions used the plural; both must keep working."""
    assert possible_values({"PossibleValues": ["Quiet", "Cool"]}) == ["Quiet", "Cool"]


def test_possible_values_singular_wins_over_plural():
    entry = {"PossibleValue": ["Quiet"], "PossibleValues": ["Quiet", "Cool", "Optimized"]}
    assert possible_values(entry) == ["Quiet"]


def test_possible_values_from_integer_bounds():
    """An IntegerAttribute has no value list, only bounds -- report the range."""
    entry = {"AttributeName": "CustomChargeStart", "LowerBound": 50, "UpperBound": 55,
             "AttributeClass": "IntegerAttribute"}
    assert possible_values(entry) == ["50", "51", "52", "53", "54", "55"]


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
                attribute("PrimaryBattChargeCfg", "Custom"),
                attribute("CustomChargeStart", "60", cls="IntegerAttribute"),
                attribute("CustomChargeStop", "80", cls="IntegerAttribute"),
            ],
        }
    )
    backend = FakeWmiBios(runner=runner)
    assert backend.read_profile() is ThermalProfile.ULTRA_PERFORMANCE
    assert backend.read_charge_config() == ChargeConfig(
        mode=ChargeMode.CUSTOM, start=60, stop=80
    )


def test_read_charge_config_simple_mode_needs_no_extra_reads():
    runner = ScriptedRunner(
        read_json={
            "interface_found": True,
            "attributes": [attribute("PrimaryBattChargeCfg", "PrimAcUse")],
        }
    )
    backend = FakeWmiBios(runner=runner)
    assert backend.read_charge_config() == ChargeConfig(mode=ChargeMode.PRIMARILY_AC)


def test_read_custom_charge_without_readable_thresholds_is_not_invented():
    """A Custom mode whose thresholds cannot be read must not be guessed at."""
    runner = ScriptedRunner(
        read_json={
            "interface_found": True,
            "attributes": [attribute("PrimaryBattChargeCfg", "Custom")],
        }
    )
    assert FakeWmiBios(runner=runner).read_charge_config() is None


def test_read_profile_returns_none_when_attribute_absent():
    runner = ScriptedRunner(read_json={"interface_found": True, "attributes": []})
    assert FakeWmiBios(runner=runner).read_profile() is None


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------
def test_set_profile_sends_exact_token():
    runner = ScriptedRunner(write_json={"status": 0})
    FakeWmiBios(runner=runner).set_profile(ThermalProfile.QUIET)

    assert runner.writes_asked() == [("ThermalManagement", "Quiet")]


def test_set_charge_sends_bare_mode_token():
    """A non-custom mode is a single plain token, e.g. ``PrimAcUse``."""
    runner = ScriptedRunner(
        read_json={
            "interface_found": True,
            "attributes": [attribute("PrimaryBattChargeCfg", "Standard", ["primacuse"])],
        }
    )
    FakeWmiBios(runner=runner).set_charge_config(ChargeConfig(mode=ChargeMode.PRIMARILY_AC))

    assert runner.writes_asked() == [("PrimaryBattChargeCfg", "PrimAcUse")]


def test_set_charge_custom_writes_thresholds_as_separate_integer_attributes():
    """Regression guard for a real hardware failure.

    On an XPS 13 7390 2-in-1 (BIOS 1.35.0) writing the combined cctk-style token
    ``Custom:60-80`` to ``PrimaryBattChargeCfg`` returns status 1 (Failed) and the
    mode does not change. What actually works is the bare ``Custom`` token plus
    the two integer attributes ``CustomChargeStart`` / ``CustomChargeStop``: each
    returns status 0 and reads back correctly.
    """
    runner = ScriptedRunner(
        read_json={
            "interface_found": True,
            "attributes": [attribute("PrimaryBattChargeCfg", "Adaptive", ["custom"])],
        }
    )
    FakeWmiBios(runner=runner).set_charge_config(
        ChargeConfig(mode=ChargeMode.CUSTOM, start=60, stop=80)
    )

    asked = runner.writes_asked()
    assert ("CustomChargeStart", "60") in asked
    assert ("CustomChargeStop", "80") in asked
    assert ("PrimaryBattChargeCfg", "Custom") in asked
    # The combined token must never be sent to the WMI interface.
    assert not any(":" in value for _, value in asked), asked
    # Mode last: the mode write is what makes the firmware adopt the thresholds.
    assert asked[-1] == ("PrimaryBattChargeCfg", "Custom")


def test_set_profile_raises_on_access_denied():
    runner = ScriptedRunner(write_statuses={"ThermalManagement": 3})
    with pytest.raises(BiosPasswordRequired):
        FakeWmiBios(runner=runner).set_profile(ThermalProfile.QUIET)


def test_set_profile_raises_on_not_supported():
    runner = ScriptedRunner(write_statuses={"ThermalManagement": 4})
    with pytest.raises(AttributeNotSupported):
        FakeWmiBios(runner=runner).set_profile(ThermalProfile.QUIET)


def test_set_profile_raises_generic_error_on_failure():
    runner = ScriptedRunner(write_statuses={"ThermalManagement": 1})
    with pytest.raises(CctkError):
        FakeWmiBios(runner=runner).set_profile(ThermalProfile.QUIET)


def test_a_partial_batch_failure_is_raised_not_swallowed():
    """If one attribute in a batch is refused, the caller must hear about it."""
    runner = ScriptedRunner(
        read_json={
            "interface_found": True,
            "attributes": [attribute("PrimaryBattChargeCfg", "Standard", ["custom"])],
        },
        write_statuses={"CustomChargeStart": 0, "CustomChargeStop": 4},
    )
    with pytest.raises(AttributeNotSupported) as excinfo:
        FakeWmiBios(runner=runner).set_charge_config(
            ChargeConfig(mode=ChargeMode.CUSTOM, start=60, stop=80)
        )
    assert "CustomChargeStop" in str(excinfo.value)


def test_bios_password_is_sent_only_when_configured():
    runner = ScriptedRunner(write_json={"status": 0})
    backend = FakeWmiBios(runner=runner, bios_password="secret123")
    backend.set_profile(ThermalProfile.QUIET)
    assert runner.write_payloads()[-1]["password"] == "secret123"

    runner2 = ScriptedRunner(write_json={"status": 0})
    FakeWmiBios(runner=runner2).set_profile(ThermalProfile.QUIET)
    assert runner2.write_payloads()[-1]["password"] is None


def test_write_script_always_sends_a_security_handle():
    """Regression guard for the real reason every write used to fail.

    ``SetAttribute`` declares SecHandle/SecHndCount/SecType before the outgoing
    ``Status``. Omitting them makes this firmware answer "Invalid parameter"
    instead of returning a status code -- so no write could ever succeed. They
    must always be sent, zero-length when there is no password.
    """
    from xpshelper.backends.wmi_bios import WRITE_SCRIPT

    assert "SecHandle" in WRITE_SCRIPT
    assert "SecHndCount" in WRITE_SCRIPT
    assert "SecType" in WRITE_SCRIPT


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


def test_write_script_uses_cim_only_and_says_why():
    """The legacy provider cannot express SetAttribute correctly.

    Invoke-WmiMethod binds positionally and cannot build the UInt8Array that
    SecHandle needs, failing with "Unable to cast object of type 'System.Int32'
    to type 'System.Array'". Rather than ship a second code path that never
    worked, writes go through CIM only -- and the script records that reasoning,
    so the next maintainer does not "helpfully" add it back.
    """
    from xpshelper.backends.wmi_bios import WRITE_SCRIPT

    assert "Invoke-CimMethod" in WRITE_SCRIPT
    # The legacy provider must not be used to invoke the method.
    assert "-Name 'SetAttribute'" not in WRITE_SCRIPT
    assert "Cannot express" in WRITE_SCRIPT or "System.Array" in WRITE_SCRIPT


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
            "namespace_exists": False,
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
    # With no namespace and no access problem, it must say the model lacks it.
    assert "does not exist on this machine" in notes


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


# ---------------------------------------------------------------------------
# "Access denied" vs "no such interface". Regression guard for a real
# misdiagnosis: on an XPS 13 7390 2-in-1 the namespace exists and the interface
# is present, but an unelevated process is refused. Reporting that as "your
# model predates WMI-ACPI support" points the user at entirely the wrong fix.
# ---------------------------------------------------------------------------
def test_access_denied_is_distinguished_from_a_missing_interface():
    runner = ScriptedRunner(
        read_json={
            "interface_found": False,
            "namespace_exists": True,
            "access_denied": True,
            "errors": ["cim:BIOSAttributeInterface: Access denied"],
        }
    )
    backend = FakeWmiBios(runner=runner)
    assert backend.access_denied() is True
    assert backend.namespace_present() is True

    note = " ".join(backend.probe().notes)
    assert "denied access" in note
    assert "administrator" in note
    # It must NOT claim the feature is absent.
    assert "predates" not in note


def test_access_denied_is_detected_from_the_error_text_alone():
    """Even if the script did not flag it, the message is enough."""
    runner = ScriptedRunner(
        read_json={"interface_found": False, "errors": ["wmi: Access is denied"]}
    )
    assert FakeWmiBios(runner=runner).access_denied() is True


def test_missing_namespace_is_not_reported_as_an_access_problem():
    runner = ScriptedRunner(
        read_json={
            "interface_found": False,
            "namespace_exists": False,
            "errors": ["cim:BIOSAttributeInterface: Invalid namespace"],
        }
    )
    backend = FakeWmiBios(runner=runner)
    assert backend.access_denied() is False
    note = " ".join(backend.probe().notes)
    assert "does not exist on this machine" in note


def test_absent_thermal_attribute_is_reported_as_a_firmware_limitation():
    """This laptop really has no ThermalManagement; the app must say so plainly."""
    runner = ScriptedRunner(
        read_json={
            "interface_found": True,
            "attributes": [
                attribute("PrimaryBattChargeCfg", "Standard",
                          ["Adaptive", "Standard", "Express", "PrimAcUse", "Custom"]),
            ],
        }
    )
    caps = FakeWmiBios(runner=runner).probe()

    assert caps.thermal_profiles == ()
    assert caps.charge_modes == (
        ChargeMode.ADAPTIVE,
        ChargeMode.STANDARD,
        ChargeMode.EXPRESS,
        ChargeMode.PRIMARILY_AC,
        ChargeMode.CUSTOM,
    )
    assert caps.supports_custom_thresholds is True
    note = " ".join(caps.notes)
    assert "does not list ThermalManagement" in note
    assert "cannot be changed on this model" in note


def test_absent_attribute_is_distinguished_from_an_unreadable_value_list():
    """Regression guard for a real UI failure.

    An empty ``thermal_profiles`` used to mean two different things: "no such
    attribute" and "attribute present but its value list was unreadable". The UI
    failed open on both, so a laptop with **no** thermal attribute offered all
    four profiles and produced a raw firmware error when one was clicked.
    ``supports_thermal``/``supports_charge`` now separate the two cases.
    """
    # Case 1: attribute absent -> explicitly unsupported.
    absent = FakeWmiBios(
        runner=ScriptedRunner(
            read_json={
                "interface_found": True,
                "attributes": [attribute("PrimaryBattChargeCfg", "Standard", ["standard"])],
            }
        )
    ).probe()
    assert absent.supports_thermal is False
    assert absent.supports_charge is True

    # Case 2: attribute present, value list unreadable -> still supported (fail open).
    unreadable = FakeWmiBios(
        runner=ScriptedRunner(
            read_json={
                "interface_found": True,
                "attributes": [attribute("ThermalManagement", "Quiet")],
            }
        )
    ).probe()
    assert unreadable.supports_thermal is True
    assert unreadable.thermal_profiles == tuple(ThermalProfile)


def test_capabilities_serialise_the_support_flags():
    runner = ScriptedRunner(
        read_json={
            "interface_found": True,
            "attributes": [attribute("PrimaryBattChargeCfg", "Standard", ["standard"])],
        }
    )
    data = FakeWmiBios(runner=runner).probe().to_dict()
    assert data["supports_thermal"] is False
    assert data["supports_charge"] is True


def test_real_firmware_payload_shape_is_parsed():
    """The exact row shape this laptop's BIOS 1.35.0 returns, verbatim."""
    runner = ScriptedRunner(
        read_json={
            "interface_found": True,
            "namespace_exists": True,
            "access_denied": False,
            "api": "cim",
            "elevated": True,
            "admin_password_set": False,
            "attributes": [
                {
                    "AttributeName": "PrimaryBattChargeCfg",
                    "CurrentValue": "Standard",
                    "PossibleValue": ["Adaptive", "Standard", "Express", "PrimAcUse", "Custom"],
                    "PossibleValueCount": 5,
                    "AttributeClass": "EnumerationAttribute",
                    "DisplayName": "Primary Battery Charge Configuration",
                    "ReadOnly": False,
                    "LowerBound": None,
                    "UpperBound": None,
                },
                {
                    "AttributeName": "CustomChargeStart",
                    "CurrentValue": "50",
                    "PossibleValue": [],
                    "PossibleValueCount": 0,
                    "AttributeClass": "IntegerAttribute",
                    "DisplayName": "Custom Charge Start",
                    "ReadOnly": False,
                    "LowerBound": 50,
                    "UpperBound": 95,
                },
            ],
        }
    )
    backend = FakeWmiBios(runner=runner)
    caps = backend.probe()

    assert caps.charge_modes == (
        ChargeMode.ADAPTIVE,
        ChargeMode.STANDARD,
        ChargeMode.EXPRESS,
        ChargeMode.PRIMARILY_AC,
        ChargeMode.CUSTOM,
    )
    assert backend.active_provider() == "cim"
    assert backend.read_charge_config() == ChargeConfig(mode=ChargeMode.STANDARD)


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
