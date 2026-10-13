"""Exception hierarchy for xps-helper.

The distinction that matters most at runtime is :class:`BiosPasswordRequired`:
Dell's firmware refuses SMBIOS writes (thermal mode, charge config) when a BIOS
password is set, and cctk reports that with a *specific* exit code rather than a
generic failure. Conflating that with "unsupported" would send users debugging
the wrong thing, so it gets its own type.
"""

from __future__ import annotations


class XpsHelperError(Exception):
    """Base class for every error this package raises."""


class UnsupportedPlatformError(XpsHelperError):
    """The requested backend cannot work on this OS."""


class BackendUnavailableError(XpsHelperError):
    """A required external component (e.g. cctk.exe) could not be located."""


class CctkError(XpsHelperError):
    """cctk.exe ran and failed for a reason we could not classify further.

    Carries the raw exit code and captured output so `doctor` can show the user
    exactly what Dell's tool said.
    """

    def __init__(self, message: str, *, exit_code: int | None = None, output: str = ""):
        super().__init__(message)
        self.exit_code = exit_code
        self.output = output


class BiosPasswordRequired(CctkError):
    """A BIOS setup/system password must be supplied before this write lands.

    Callers should surface this as "set a BIOS password in the app settings",
    not as a crash.
    """


class BiosPasswordInvalid(CctkError):
    """The supplied BIOS password was rejected."""


class AttributeNotSupported(CctkError):
    """The BIOS attribute exists in Dell's CLI but not on this exact model.

    cctk validates attributes against the SMBIOS tables, so a 7390 may accept
    ThermalManagement but reject, say, some advanced charge option.
    """


class ValidationError(XpsHelperError):
    """A value failed our client-side range/sanity checks before touching hardware."""
