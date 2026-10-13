"""Service-layer tests: persistence, write verification, degradation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from xpshelper.backends.base import NullControlBackend
from xpshelper.backends.mock import MockBackend
from xpshelper.errors import ValidationError, XpsHelperError
from xpshelper.model import (
    Capabilities,
    ChargeConfig,
    ChargeMode,
    ThermalProfile,
)
from xpshelper.service import ConfigStore, Settings, XpsHelper


class SilentFailureControl:
    """Accepts writes but always reports something else.

    Simulates the real-world case where Dell's firmware declines a write (for
    example a profile change while a Thunderbolt dock is attached) without
    returning an error code.
    """

    name = "stub"

    def __init__(
        self,
        actual_profile: ThermalProfile | None = ThermalProfile.OPTIMIZED,
        actual_charge: ChargeConfig | None = None,
    ) -> None:
        self.actual_profile = actual_profile
        self.actual_charge = actual_charge or ChargeConfig(mode=ChargeMode.ADAPTIVE)
        self.writes: list[tuple[str, str]] = []

    def probe(self) -> Capabilities:
        return Capabilities(
            control_backend=self.name,
            thermal_profiles=tuple(ThermalProfile),
            charge_modes=tuple(ChargeMode),
        )

    def read_profile(self):
        return self.actual_profile

    def read_charge_config(self):
        return self.actual_charge

    def set_profile(self, profile) -> None:
        self.writes.append(("profile", profile.value))

    def set_charge_config(self, config) -> None:
        self.writes.append(("charge", config.token))


# ---------------------------------------------------------------------------
# Write verification -- a write is not successful until it is read back.
# ---------------------------------------------------------------------------
def test_silent_profile_failure_is_detected(tmp_store):
    control = SilentFailureControl(actual_profile=ThermalProfile.OPTIMIZED)
    service = XpsHelper(control=control, telemetry=MockBackend(), store=tmp_store)

    with pytest.raises(XpsHelperError, match="did not take effect"):
        service.set_profile(ThermalProfile.QUIET)
    assert control.writes == [("profile", "Quiet")]


def test_silent_charge_failure_is_detected(tmp_store):
    control = SilentFailureControl(actual_charge=ChargeConfig(mode=ChargeMode.ADAPTIVE))
    service = XpsHelper(control=control, telemetry=MockBackend(), store=tmp_store)

    with pytest.raises(XpsHelperError, match="did not take effect"):
        service.set_charge(ChargeMode.STANDARD)


def test_threshold_mismatch_is_detected(tmp_store):
    control = SilentFailureControl(
        actual_charge=ChargeConfig(mode=ChargeMode.CUSTOM, start=50, stop=60)
    )
    service = XpsHelper(control=control, telemetry=MockBackend(), store=tmp_store)

    with pytest.raises(XpsHelperError, match="Thresholds did not stick"):
        service.set_charge(ChargeMode.CUSTOM, start=60, stop=80)


def test_verification_can_be_skipped(tmp_store):
    control = SilentFailureControl(actual_profile=ThermalProfile.OPTIMIZED)
    service = XpsHelper(control=control, telemetry=MockBackend(), store=tmp_store)
    service.set_profile(ThermalProfile.QUIET, verify=False)
    assert control.writes == [("profile", "Quiet")]


# ---------------------------------------------------------------------------
# Unsupported values are rejected before touching the firmware.
# ---------------------------------------------------------------------------
def test_unsupported_profile_rejected_without_writing(tmp_store):
    control = MockBackend(profiles=(ThermalProfile.OPTIMIZED, ThermalProfile.QUIET))
    service = XpsHelper(control=control, telemetry=control, store=tmp_store)

    with pytest.raises(ValidationError, match="not supported"):
        service.set_profile(ThermalProfile.ULTRA_PERFORMANCE)
    assert control.write_log == []


def test_unsupported_charge_mode_rejected_without_writing(tmp_store):
    control = MockBackend(modes=(ChargeMode.STANDARD, ChargeMode.ADAPTIVE))
    service = XpsHelper(control=control, telemetry=control, store=tmp_store)

    with pytest.raises(ValidationError, match="not supported"):
        service.set_charge(ChargeMode.CUSTOM, start=60, stop=80)
    assert control.write_log == []


def test_unknown_profile_name_is_a_validation_error(tmp_store, mock_service):
    with pytest.raises(ValidationError, match="Unknown thermal profile"):
        mock_service.set_profile("ludicrous")


def test_custom_without_thresholds_is_a_validation_error(mock_service):
    with pytest.raises(ValidationError, match="needs both"):
        mock_service.set_charge("custom")


def test_charge_accepts_full_token_string(tmp_store):
    control = MockBackend()
    service = XpsHelper(control=control, telemetry=control, store=tmp_store)
    applied = service.set_charge("custom:65-85")
    assert applied == ChargeConfig(mode=ChargeMode.CUSTOM, start=65, stop=85)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def test_set_profile_persists(tmp_store):
    control = MockBackend()
    service = XpsHelper(control=control, telemetry=control, store=tmp_store)
    service.set_profile(ThermalProfile.COOL)

    reloaded = ConfigStore(tmp_store.path).load()
    assert reloaded.profile == "Cool"


def test_set_charge_persists_thresholds(tmp_store):
    control = MockBackend()
    service = XpsHelper(control=control, telemetry=control, store=tmp_store)
    service.set_charge(ChargeMode.CUSTOM, start=60, stop=80)

    reloaded = ConfigStore(tmp_store.path).load()
    assert reloaded.charge_mode == "Custom"
    assert reloaded.charge_start == 60
    assert reloaded.charge_stop == 80
    assert reloaded.as_charge_config() == ChargeConfig(
        mode=ChargeMode.CUSTOM, start=60, stop=80
    )


def test_mock_state_follows_saved_settings_across_processes(tmp_path):
    """`--backend mock` must not look broken between CLI invocations.

    The mock's state is in memory, so a fresh process would otherwise report the
    default profile right after the user set a different one.
    """
    store_path = tmp_path / "config.json"

    first = XpsHelper(control=MockBackend(), telemetry=MockBackend(), store=ConfigStore(store_path))
    first.set_profile(ThermalProfile.QUIET)
    first.set_charge(ChargeMode.CUSTOM, start=60, stop=80)

    second = XpsHelper(control=MockBackend(), telemetry=MockBackend(), store=ConfigStore(store_path))
    assert second.control.read_profile() is ThermalProfile.QUIET
    assert second.control.read_charge_config() == ChargeConfig(
        mode=ChargeMode.CUSTOM, start=60, stop=80
    )


def test_apply_saved_respects_flags(tmp_store):
    control = MockBackend()
    service = XpsHelper(control=control, telemetry=control, store=tmp_store)
    service.update_settings(
        profile="Quiet",
        charge_mode="PrimAcUse",
        apply_profile_on_startup=True,
        apply_charge_on_startup=False,
    )
    control.write_log.clear()

    messages = service.apply_saved()

    assert any("Quiet" in message for message in messages)
    assert control.write_log == [("ThermalManagement", "Quiet")]


def test_apply_saved_with_nothing_configured(mock_service):
    assert mock_service.apply_saved() == []


def test_update_settings_rejects_unknown_key(mock_service):
    with pytest.raises(ValidationError, match="Unknown setting"):
        mock_service.update_settings(nonsense=1)


# ---------------------------------------------------------------------------
# Settings robustness
# ---------------------------------------------------------------------------
def test_settings_tolerates_unknown_and_wrong_typed_fields():
    settings = Settings.from_dict(
        {"profile": "Quiet", "future_option": True, "refresh_seconds": "not-a-number"}
    )
    # Unknown keys are ignored rather than crashing on an older/newer config.
    assert settings.profile == "Quiet"
    assert not hasattr(settings, "future_option")


def test_settings_clamps_absurd_refresh_interval():
    assert Settings.from_dict({"refresh_seconds": 0}).refresh_seconds == 1
    assert Settings.from_dict({"refresh_seconds": -5}).refresh_seconds == 1


def test_invalid_stored_charge_config_is_ignored():
    settings = Settings.from_dict({"charge_mode": "Custom", "charge_start": 10, "charge_stop": 20})
    assert settings.as_charge_config() is None


def test_unknown_stored_charge_mode_is_ignored():
    settings = Settings.from_dict({"charge_mode": "Bogus"})
    assert settings.as_charge_config() is None


def test_config_store_handles_corrupt_json(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{ this is not json", encoding="utf-8")
    assert ConfigStore(path).load() == Settings()


def test_config_store_handles_missing_file(tmp_path):
    assert ConfigStore(tmp_path / "nope.json").load() == Settings()


def test_config_store_round_trip(tmp_path):
    path = tmp_path / "config.json"
    store = ConfigStore(path)
    store.save(Settings(profile="Cool", refresh_seconds=7))

    assert json.loads(path.read_text(encoding="utf-8"))["profile"] == "Cool"
    assert ConfigStore(path).load().refresh_seconds == 7


# ---------------------------------------------------------------------------
# Degradation
# ---------------------------------------------------------------------------
def test_null_control_backend_gives_actionable_error(tmp_store):
    service = XpsHelper(
        control=NullControlBackend("cctk is not installed."),
        telemetry=MockBackend(),
        store=tmp_store,
    )
    with pytest.raises(XpsHelperError, match="cctk is not installed"):
        service.set_profile(ThermalProfile.QUIET)


def test_status_still_works_without_control(tmp_store):
    service = XpsHelper(
        control=NullControlBackend("no cctk"),
        telemetry=MockBackend(),
        store=tmp_store,
    )
    status = service.status()
    assert status.battery is not None
    assert status.profile is None
    assert status.cpu_temperature is not None


def test_power_state_reports_plugged_and_percent(tmp_store):
    control = MockBackend(plugged_in=False, battery_percent=42)
    service = XpsHelper(control=control, telemetry=control, store=tmp_store)
    assert service.power_state() == (False, 42)


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------
def test_capabilities_include_telemetry_availability(mock_service):
    caps = mock_service.capabilities(refresh=True)
    assert caps.battery_readable
    assert caps.temperatures_readable
    assert caps.fans_readable
    assert caps.control_backend == "mock"


def test_capabilities_reflect_missing_telemetry(tmp_store):
    control = MockBackend()
    telemetry = MockBackend(battery=False, temperatures=False, fans=False)
    service = XpsHelper(control=control, telemetry=telemetry, store=tmp_store)
    caps = service.capabilities(refresh=True)
    assert not caps.battery_readable
    assert not caps.temperatures_readable
    assert not caps.fans_readable
