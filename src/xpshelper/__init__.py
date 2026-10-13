"""XPS Helper -- thermal profile and battery charge control for Dell XPS laptops.

Public API::

    from xpshelper import make_service

    svc = make_service()                      # auto-detect backends
    svc.set_profile("quiet")                  # writes + verifies + persists
    svc.set_charge("custom", start=60, stop=80)
    print(svc.status().battery.percent)
"""

from __future__ import annotations

from .constants import APP_DISPLAY_NAME, APP_NAME, APP_VERSION
from .errors import (
    AttributeNotSupported,
    BackendUnavailableError,
    BiosPasswordInvalid,
    BiosPasswordRequired,
    CctkError,
    UnsupportedPlatformError,
    ValidationError,
    XpsHelperError,
)
from .model import (
    BatteryStatus,
    Capabilities,
    ChargeConfig,
    ChargeMode,
    FanReading,
    SystemStatus,
    TemperatureReading,
    ThermalProfile,
)
from .service import ConfigStore, Settings, XpsHelper, make_service

__all__ = [
    "APP_DISPLAY_NAME",
    "APP_NAME",
    "APP_VERSION",
    "AttributeNotSupported",
    "BackendUnavailableError",
    "BatteryStatus",
    "BiosPasswordInvalid",
    "BiosPasswordRequired",
    "Capabilities",
    "CctkError",
    "ChargeConfig",
    "ChargeMode",
    "ConfigStore",
    "FanReading",
    "Settings",
    "SystemStatus",
    "TemperatureReading",
    "ThermalProfile",
    "UnsupportedPlatformError",
    "ValidationError",
    "XpsHelper",
    "XpsHelperError",
    "make_service",
]
