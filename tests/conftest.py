"""Shared test fixtures.

The important one is :class:`FakeRunner`: it lets the *real* cctk backend run
against canned Dell output, so the parsing and exit-code handling that will face
actual firmware are exercised in CI rather than only on the laptop.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Sequence

import pytest

from xpshelper import constants as C
from xpshelper import logsetup
from xpshelper.backends.cctk import CctkBackend, CctkResult
from xpshelper.backends.mock import MockBackend
from xpshelper.service import ConfigStore, XpsHelper


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Keep tests away from the real user config and log files.

    Also resets the logging singleton so each test's file handler points inside
    its own tmp_path instead of a directory pytest has already deleted (which
    would otherwise spray "--- Logging error ---" into the test output).
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    monkeypatch.delenv(C.ENV_BIOS_PASSWORD, raising=False)
    monkeypatch.delenv(C.ENV_DEBUG, raising=False)

    logsetup._CONFIGURED = False
    root = logging.getLogger("xpshelper")
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    logsetup._CONFIGURED = False


@pytest.fixture(autouse=True)
def no_privileged_helper(monkeypatch: pytest.MonkeyPatch):
    """Pretend no privileged helper is installed, for every test.

    Once the helper is installed on the developer's machine, charge writes are
    routed to it, which silently bypasses the in-process validation and firmware
    error paths many tests exist to check. Those tests then fail for an
    environmental reason rather than a real one.

    Tests that exercise the helper itself re-patch these, so this only establishes
    a deterministic default.
    """
    from xpshelper import privileged

    monkeypatch.setattr(
        privileged, "is_installed", lambda task_name=privileged.TASK_NAME: False
    )
    monkeypatch.delenv(privileged.ENV_PRIVILEGED_CHILD, raising=False)
    yield


# ---------------------------------------------------------------------------
# Realistic cctk help text. Format taken from the hardware-tested
# dell-powermanager parser: an "Arguments:" line, values separated by "|",
# with "+" marking the values this model accepts.
# ---------------------------------------------------------------------------
THERMAL_HELP_FULL = """\
Usage: cctk --ThermalManagement=<value>

Arguments:
 optimized+ | quiet+ | cool+ | ultraperformance+
"""

THERMAL_HELP_PARTIAL = """\
Usage: cctk --ThermalManagement=<value>

Arguments:
 optimized+ | quiet+ | cool | ultraperformance+
"""

CHARGE_HELP_FULL = """\
Usage: cctk --PrimaryBattChargeCfg=<value>

Arguments:
 standard+ | express+ | primacuse+ | adaptive+ | custom+
"""


class FakeRunner:
    """Maps cctk argv to canned responses by substring match.

    Responses are checked in insertion order, so put more specific keys first.
    A value may be an int (exit code), a ``(exit_code, stdout)`` pair, or a
    ``(exit_code, stdout, stderr)`` triple.
    """

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.responses: dict[str, Any] = dict(responses or {})
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: Sequence[str], timeout: int) -> CctkResult:
        self.calls.append(tuple(argv))
        joined = " ".join(argv)
        for key, spec in self.responses.items():
            if key in joined:
                code, out, err = _normalise_spec(spec)
                return CctkResult(tuple(argv), code, out, err)
        return CctkResult(tuple(argv), 0, "", "")

    @property
    def wrote(self) -> list[str]:
        """Arguments of every invocation that looked like a write."""
        return [" ".join(call) for call in self.calls if "=" in " ".join(call)]


def _normalise_spec(spec: Any) -> tuple[int, str, str]:
    if isinstance(spec, int):
        return spec, "", ""
    if isinstance(spec, tuple):
        if len(spec) == 2:
            return spec[0], spec[1], ""
        if len(spec) == 3:
            return spec[0], spec[1], spec[2]
    raise TypeError(f"unsupported fake response spec: {spec!r}")


@pytest.fixture
def fake_runner() -> FakeRunner:
    return FakeRunner()


def build_cctk(tmp_path: Path, responses: dict[str, Any] | None = None, **kwargs: Any):
    """Create a CctkBackend backed by canned output instead of real hardware."""
    exe = tmp_path / "cctk.exe"
    exe.write_text("stub executable for tests", encoding="utf-8")
    runner = FakeRunner(responses)
    backend = CctkBackend(path=exe, runner=runner, **kwargs)
    return backend, runner


@pytest.fixture
def tmp_store(tmp_path: Path) -> ConfigStore:
    return ConfigStore(tmp_path / "config.json")


@pytest.fixture
def mock_service(tmp_store: ConfigStore) -> XpsHelper:
    return XpsHelper(control=MockBackend(), telemetry=MockBackend(), store=tmp_store)
