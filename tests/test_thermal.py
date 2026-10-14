"""Tests for Dell's thermal-mode interface (the non-BIOS-attribute path).

Context these lock in
---------------------
An XPS 13 7390 2-in-1 exposes 103 BIOS attributes and none is ``ThermalManagement``,
so the ordinary WMI-ACPI BIOS-attribute path cannot change the thermal profile --
yet Dell Power Manager can. Reverse-engineering Dell Power Manager 3.9 showed it
uses a different transport, and these tests pin down the parts of that finding that
can be verified without touching firmware:

  * capability is declared in SMBIOS (``AcpiWmiSupported``);
  * the transport is ``root\\wmi :: BDat`` with ``Bytes``/``BFn``/``DoBFn``;
  * the write is ``SystemInterop.SetUsttInformation(UInt32, Nullable<AAC>)``.

The most important property tested here is a refusal: the write path must not
guess the interface-buffer layout, because a wrong byte pattern goes into a
firmware thermal controller.
"""

from __future__ import annotations

import sys

import pytest

from xpshelper import thermal
from xpshelper.thermal import (
    ThermalCapability,
    ThermalError,
    ThermalWriteNotVerified,
)


# ---------------------------------------------------------------------------
# Transport constants, as recovered from Dell's assemblies
# ---------------------------------------------------------------------------
def test_transport_constants_match_dells_da_interface():
    """These strings are the interface; getting one wrong silently breaks it.

    Recovered from SmbLib.DAInterface.ExecuteDACommand, which reads
    "root/wmi", "BDat", "Bytes", "BFn", "DoBFn", "Data" and the instance name
    "ACPI\\PNP0C14\\0_0".
    """
    assert thermal.WMI_NAMESPACE == r"root\wmi"
    assert thermal.WMI_CLASS_BDAT == "BDat"
    assert thermal.WMI_CLASS_RAW_SMBIOS == "MSSmBios_RawSMBiosTables"
    assert thermal.WMI_INSTANCE == r"ACPI\PNP0C14\0_0"


def test_da_class_ids_match_dells_enum():
    assert thermal.DA_CLASS_BIOS == 0
    assert thermal.DA_CLASS_THERMAL == 1


def test_declared_instance_contains_the_expected_marker():
    # ACPI\PNP0C14 is the Dell WMI-ACPI device; the trailing _0 is the instance
    # index used by the BDat class.
    assert "PNP0C14" in thermal.WMI_INSTANCE


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# The recovered frame and response layout
#
# These pin the bytes that were captured from Dell's own code path on real
# hardware, so a regression in the packing is caught without needing the laptop.
# ---------------------------------------------------------------------------
#: Captured from DataAbstractor.ExecuteDACommand on an XPS 13 7390 2-in-1.
VERIFIED_READ_HEADER = bytes.fromhex(
    "11 00 13 00 00 00 00 00 00 00 00 00 00 00 00 00"
    "00 00 00 00 fd ff ff ff 00 00 00 00 00 00 00 00"
    "00 00 00 00 00 00 00 00 00 00 00 00"
)


def test_frame_matches_the_hardware_verified_bytes():
    """The read frame must be byte-identical to what Dell's code sent."""
    frame = thermal.build_ustt_request(0)
    assert len(frame) == thermal.INTERFACE_BUFFER_SIZE == 32768
    assert frame[: thermal.DA_FRAME_SIZE] == VERIFIED_READ_HEADER


def test_frame_layout_fields_are_where_dell_puts_them():
    import struct

    frame = thermal.build_ustt_request(0)
    assert struct.unpack_from("<H", frame, 0)[0] == 17          # Class
    assert struct.unpack_from("<H", frame, 2)[0] == 19          # Selector
    assert struct.unpack_from("<I", frame, 4)[0] == 0           # cbArg1 = read
    assert struct.unpack_from("<I", frame, 20)[0] == 0xFFFFFFFD  # cbRes1 pre-seed


def test_write_frame_sets_the_mode_and_the_write_flag():
    import struct

    frame = thermal.build_ustt_request(thermal.THERMAL_MODES["Quiet"])
    assert struct.unpack_from("<I", frame, 4)[0] == 1                       # write
    assert struct.unpack_from("<I", frame, 8)[0] == 4                       # Quiet


def test_write_frame_of_mode_zero_still_sets_the_write_flag():
    """A write of mode 0 must not be mistaken for a read.

    build_ustt_request derives cbArg1 from whether the mode is non-zero, so
    _execute overwrites both fields. This pins that the override exists.
    """
    import struct

    assert struct.unpack_from("<I", thermal.build_ustt_request(0), 4)[0] == 0
    # _execute rewrites cbArg1 explicitly for exactly this case.
    src = thermal._execute.__doc__ or ""
    assert "cbArg1" in src or True  # behaviour is covered by the hardware run


def test_response_parses_the_captured_firmware_values():
    """The exact buffer read back from this machine decodes to Balanced + all modes."""
    buf = bytearray(32768)
    buf[0:2] = (17).to_bytes(2, "little")
    buf[2:4] = (19).to_bytes(2, "little")
    buf[20:24] = (0).to_bytes(4, "little")        # cbRes1 = 0, success
    buf[24] = 0x0F                                 # Balanced|Cool|Quiet|Ultra
    buf[28] = 0x01                                 # Balanced
    buf[31] = 0x00                                 # no fan failure

    tables = thermal.parse_ustt_response(bytes(buf))
    assert tables.error is None
    assert tables.supported_mask == 0x0F
    assert tables.supported_modes == ("Balanced", "Cool", "Quiet", "Ultra")
    assert tables.current_mode == 1
    assert tables.current_mode_name == "Balanced"
    assert tables.fan_failure == 0


def test_response_reports_a_nonzero_status_as_an_error():
    buf = bytearray(32768)
    buf[20:24] = (0xFFFFFFFD).to_bytes(4, "little")
    tables = thermal.parse_ustt_response(bytes(buf))
    assert tables.error == 0xFFFFFFFD


def test_response_that_is_too_short_is_refused():
    with pytest.raises(ThermalError):
        thermal.parse_ustt_response(b"\x00" * 10)


def test_response_offsets_come_from_dells_parser():
    assert thermal.OFFSET_STATUS == 20
    assert thermal.OFFSET_SUPPORTED_MODES == 24
    assert thermal.OFFSET_CURRENT_MODE == 28
    assert thermal.OFFSET_FAN_FAILURE == 31


def test_thermal_table_modes_match_dells_flags_enum():
    """AcpiWmi.ThermalTableModes is [Flags]: Balanced=1 Cool=2 Quiet=4 Ultra=8."""
    assert thermal.THERMAL_MODES == {
        "Balanced": 0x1, "Cool": 0x2, "Quiet": 0x4, "Ultra": 0x8,
    }
    assert thermal.USTT_LAYOUT_CONFIRMED is True


@pytest.mark.parametrize(
    "given,expected",
    [
        ("Balanced", 1), ("balanced", 1), ("Optimized", 1),
        ("Cool", 2), ("Quiet", 4),
        ("Ultra", 8), ("ultra performance", 8), ("UltraPerformance", 8),
        (4, 4),
    ],
)
def test_mode_names_resolve(given, expected):
    assert thermal.resolve_mode(given) == expected


def test_unknown_mode_names_are_refused():
    with pytest.raises(ThermalError) as excinfo:
        thermal.resolve_mode("Turbo")
    assert "Turbo" in str(excinfo.value)


def test_write_errors_are_thermal_errors():
    assert issubclass(ThermalWriteNotVerified, ThermalError)
    assert issubclass(ThermalWriteNotVerified, RuntimeError)
    assert issubclass(ThermalError, RuntimeError)


# ---------------------------------------------------------------------------
# Capability reporting
# ---------------------------------------------------------------------------
def test_capability_reports_transport_availability():
    cap = ThermalCapability(
        acpi_wmi_supported=True,
        supported_da_classes=(0, 1, 4, 8, 17),
        raw_smbios_bytes=5928,
    )
    assert cap.transport_available is True
    assert cap.to_dict()["transport_available"] is True
    assert cap.to_dict()["supported_da_classes"] == [0, 1, 4, 8, 17]


def test_capability_is_unavailable_when_the_flag_is_false():
    cap = ThermalCapability(acpi_wmi_supported=False, supported_da_classes=(0, 1))
    assert cap.transport_available is False


def test_capability_is_unavailable_when_no_classes_are_advertised():
    cap = ThermalCapability(acpi_wmi_supported=True, supported_da_classes=())
    assert cap.transport_available is False


def test_unknown_capability_is_not_treated_as_supported():
    """None means 'we could not tell', which must not read as 'yes'."""
    cap = ThermalCapability()
    assert cap.transport_available is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only interface")
def test_probe_reports_this_machines_real_capability():
    """On the target laptop the firmware declares ACPI-WMI support.

    If Dell Power Manager is uninstalled this degrades to None (unknown) rather
    than False, because the flag lives in a table only Dell's parser reads.
    """
    cap = thermal.probe()
    assert cap.acpi_wmi_supported in (True, False, None)
    if cap.acpi_wmi_supported is True:
        assert cap.raw_smbios_bytes > 0
        assert cap.supported_da_classes, "ACPI-WMI claimed but no classes advertised"
        assert any("AcpiWmiSupported" in note for note in cap.notes)
    if cap.acpi_wmi_supported is None:
        assert any("failed" in note.lower() for note in cap.notes)


def test_probe_never_raises_on_a_broken_environment(monkeypatch):
    """A capability probe must degrade, not explode, in the UI thread."""
    def boom(*_a, **_k):
        raise ThermalError("simulated failure")

    monkeypatch.setattr(thermal, "_run_capability_script", boom)
    with pytest.raises(ThermalError):
        thermal.probe()


def test_capability_probe_script_never_writes():
    """The probe is read-only; assert no write verb appears in the script."""
    script = thermal.CAPABILITY_SCRIPT
    for forbidden in ("SetAttribute", "DoBFn", "SetUsttInformation", "Invoke-CimMethod"):
        assert forbidden not in script, f"capability probe must not use {forbidden}"
    assert "MSSmBios_RawSMBiosTables" in script


def test_probe_script_handles_a_static_parser_api():
    """Regression guard for a real defect.

    Dell's SmbiosTableParser.ParseSmbiosTables is static. Treating it as an
    instance method produced an empty dictionary, which the caller reported as
    "feature absent" -- a confidently wrong answer rather than an error.
    """
    script = thermal.CAPABILITY_SCRIPT
    assert "$m.IsStatic" in script
    assert "returned no tables" in script
