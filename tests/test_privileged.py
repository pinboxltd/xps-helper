"""Tests for the zero-UAC privilege mechanism.

The design: register an elevated Scheduled Task **once**, then trigger it with
``schtasks /run`` — which needs no UAC prompt, because the elevation decision was
made at registration time. This is the same shape Dell uses (an already-elevated
service the unelevated UI delegates to), without depending on Dell's software.

What is worth testing here is mostly the *refusal* behaviour: a privileged helper
that can be triggered with stale or malformed input is a security problem, so each
of those paths is asserted to fail closed.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from xpshelper import privileged as priv


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------
def test_helper_command_is_absolute_and_has_no_user_input():
    """The task action is a fixed command line; nothing user-supplied may enter it."""
    command = priv.helper_command()
    assert command, "expected a command"
    from pathlib import Path

    assert Path(command[0]).is_absolute(), command[0]
    assert command[1:] == ["-m", "xpshelper.elevated_run"]


def test_install_script_registers_at_highest_run_level():
    script = priv.install_script()
    assert "Register-ScheduledTask" in script
    assert "RunLevel Highest" in script
    assert priv.TASK_NAME in script


def test_install_script_creates_no_trigger():
    """A trigger would re-run privileged work unprompted; the task must be inert.

    Asserted structurally: only the comment may mention a trigger.
    """
    script = priv.install_script()
    for forbidden in ("New-ScheduledTaskTrigger", "AtLogOn", "AtStartup", "AtBoot"):
        assert forbidden not in script, f"the helper task must not use {forbidden}"


def test_install_script_embeds_the_action_not_a_placeholder():
    script = priv.install_script()
    assert priv.helper_command()[0] in script
    assert "-m xpshelper.elevated_run" in script


def test_task_name_is_quoted_safely():
    """A quote in the task name must not be able to break out of the script."""
    script = priv.install_script("Evil'; Remove-Item C:\\ -Recurse; '")
    assert "Remove-Item C:\\ -Recurse" not in script or "''" in script
    # The name is embedded with single quotes doubled.
    assert "Evil''; Remove-Item" in script


def test_uninstall_script_targets_the_task():
    script = priv.uninstall_script()
    assert "Unregister-ScheduledTask" in script
    assert priv.TASK_NAME in script


# ---------------------------------------------------------------------------
# Request-file protocol
# ---------------------------------------------------------------------------
def test_request_path_is_stable_and_per_user(tmp_path, monkeypatch):
    """Parent and child must derive the same path without being told it.

    A Scheduled Task's action cannot vary per invocation, so the path has to be
    derivable rather than passed.
    """
    monkeypatch.setattr(priv.tempfile, "gettempdir", lambda: str(tmp_path))
    first = priv.request_path()
    second = priv.request_path()
    assert first == second
    assert first.parent.exists()
    assert first.name.startswith("request-")


def test_request_path_contains_no_user_supplied_text(monkeypatch):
    monkeypatch.setattr(priv, "_client_id", lambda: "S-1-5-21-1-2-3-1000")
    assert ".." not in priv.request_path().name
    assert "/" not in priv.request_path().name
    assert "\\" not in priv.request_path().name


class TestReadRequestFailsClosed:
    """Every bad-input path must refuse, never run."""

    def _use(self, tmp_path, monkeypatch):
        monkeypatch.setattr(priv.tempfile, "gettempdir", lambda: str(tmp_path))
        return priv.request_path()

    def test_missing_file(self, tmp_path, monkeypatch):
        self._use(tmp_path, monkeypatch)
        with pytest.raises(priv.HelperError) as excinfo:
            priv.read_request()
        assert "no pending privileged request" in str(excinfo.value)

    def test_malformed_json(self, tmp_path, monkeypatch):
        path = self._use(tmp_path, monkeypatch)
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(priv.HelperError):
            priv.read_request()

    def test_wrong_shape(self, tmp_path, monkeypatch):
        path = self._use(tmp_path, monkeypatch)
        path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
        with pytest.raises(priv.HelperError):
            priv.read_request()

    def test_empty_command_list(self, tmp_path, monkeypatch):
        path = self._use(tmp_path, monkeypatch)
        path.write_text(json.dumps({"command": []}), encoding="utf-8")
        with pytest.raises(priv.HelperError) as excinfo:
            priv.read_request()
        assert "no command" in str(excinfo.value)

    def test_stale_request_is_refused(self, tmp_path, monkeypatch):
        """A leftover file must not be replayable by anyone able to start the task."""
        path = self._use(tmp_path, monkeypatch)
        path.write_text(json.dumps({"command": ["cmd", "/c", "echo hi"]}), encoding="utf-8")
        old = time.time() - (priv.REQUEST_MAX_AGE_SECONDS + 60)
        os.utime(path, (old, old))
        with pytest.raises(priv.HelperError) as excinfo:
            priv.read_request()
        assert "stale" in str(excinfo.value)

    def test_fresh_valid_request_is_returned(self, tmp_path, monkeypatch):
        path = self._use(tmp_path, monkeypatch)
        path.write_text(json.dumps({"command": ["cmd", "/c", "echo hi"]}), encoding="utf-8")
        assert priv.read_request()["command"] == ["cmd", "/c", "echo hi"]

    def test_clear_removes_the_file(self, tmp_path, monkeypatch):
        path = self._use(tmp_path, monkeypatch)
        path.write_text("{}", encoding="utf-8")
        priv.clear_request()
        assert not path.exists()

    def test_clear_is_idempotent(self, tmp_path, monkeypatch):
        self._use(tmp_path, monkeypatch)
        priv.clear_request()
        priv.clear_request()  # must not raise


# ---------------------------------------------------------------------------
# Using the helper
# ---------------------------------------------------------------------------
def test_run_via_helper_refuses_when_not_installed(monkeypatch):
    """The important safety property: no silent fallback to a UAC prompt."""
    monkeypatch.setattr(priv, "is_installed", lambda task_name=priv.TASK_NAME: False)
    with pytest.raises(priv.HelperNotInstalled) as excinfo:
        priv.run_via_helper(["cmd", "/c", "echo hi"])
    assert "UAC prompt" in str(excinfo.value)


def test_is_installed_is_false_off_windows(monkeypatch):
    monkeypatch.setattr(priv.sys, "platform", "linux")
    assert priv.action_target_exists() is False


def test_describe_reports_prompt_behaviour(monkeypatch):
    monkeypatch.setattr(priv, "is_installed", lambda task_name=priv.TASK_NAME: True)
    state = priv.describe()
    assert state["installed"] is True
    assert state["prompts_per_use"] == 0

    monkeypatch.setattr(priv, "is_installed", lambda task_name=priv.TASK_NAME: False)
    assert priv.describe()["prompts_per_use"] is None


def test_helper_result_ok_reflects_exit_code():
    assert priv.HelperResult(exit_code=0).ok is True
    assert priv.HelperResult(exit_code=1).ok is False
    assert priv.HelperResult(exit_code=1).exit_code == 1


def test_helper_errors_are_runtime_errors():
    assert issubclass(priv.HelperError, RuntimeError)
    assert issubclass(priv.HelperNotInstalled, priv.HelperError)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only mechanism")
def test_run_via_helper_full_round_trip(monkeypatch):
    """End-to-end of the helper protocol, with the elevated child simulated.

    Verifies the whole contract without needing a real elevated task: the request
    is written where both halves expect it, the child's three result files are
    picked up, the exit code is honoured, and the request is cleaned up.
    """
    from pathlib import Path

    monkeypatch.setattr(priv, "is_installed", lambda task_name=priv.TASK_NAME: True)
    seen: dict = {}

    def fake_run(argv, **_kwargs):
        class Result:
            returncode = 0
            stdout = "SUCCESS: task run"
            stderr = ""

        if argv[:2] == ["schtasks", "/run"]:
            request = json.loads(Path(priv.request_path()).read_text(encoding="utf-8"))
            seen["command"] = request["command"]
            Path(request["out_file"]).write_text("done\n", encoding="utf-8")
            Path(request["err_file"]).write_text("", encoding="utf-8")
            priv.clear_request()  # the child removes it after reading
            Path(request["code_file"]).write_text("0\n", encoding="utf-8")
        return Result()

    monkeypatch.setattr(priv.subprocess, "run", fake_run)
    result = priv.run_via_helper([sys.executable, "-m", "xpshelper", "charge", "Custom"])

    assert result.ok is True
    assert result.stdout == "done\n"
    assert seen["command"][-1] == "Custom"
    assert not priv.request_path().exists(), "the request must be cleaned up"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only mechanism")
def test_run_via_helper_surfaces_a_nonzero_child_exit(monkeypatch):
    """A refused write must not look like a successful one."""
    from pathlib import Path

    monkeypatch.setattr(priv, "is_installed", lambda task_name=priv.TASK_NAME: True)

    def fake_run(argv, **_kwargs):
        class Result:
            stdout = ""
            stderr = ""

        if argv[:2] == ["schtasks", "/run"]:
            request = json.loads(Path(priv.request_path()).read_text(encoding="utf-8"))
            Path(request["out_file"]).write_text("", encoding="utf-8")
            Path(request["err_file"]).write_text("access denied\n", encoding="utf-8")
            priv.clear_request()
            Path(request["code_file"]).write_text("1\n", encoding="utf-8")
        Result.returncode = 0
        return Result()

    monkeypatch.setattr(priv.subprocess, "run", fake_run)
    result = priv.run_via_helper(["cmd", "/c", "exit 1"])
    assert result.ok is False
    assert result.exit_code == 1
    assert "access denied" in result.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only mechanism")
def test_run_via_helper_reports_when_the_child_never_answers(monkeypatch):
    """Silence must be an error, never an assumed success."""
    monkeypatch.setattr(priv, "is_installed", lambda task_name=priv.TASK_NAME: True)

    def fake_run(argv, **_kwargs):
        class Result:
            returncode = 0
            stdout = "SUCCESS: task run"
            stderr = ""

        return Result()  # never writes the sentinel

    monkeypatch.setattr(priv.subprocess, "run", fake_run)
    # DEFAULT_TIMEOUT is a parameter default, evaluated at def time, so patching
    # the module attribute would not affect it -- pass the timeout explicitly.
    with pytest.raises(priv.HelperError) as excinfo:
        priv.run_via_helper(["cmd", "/c", "echo hi"], timeout=0.3)
    assert "did not report back" in str(excinfo.value)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only mechanism")
def test_run_via_helper_reports_a_failed_task_start(monkeypatch):
    monkeypatch.setattr(priv, "is_installed", lambda task_name=priv.TASK_NAME: True)

    def fake_run(argv, **_kwargs):
        class Result:
            returncode = 1
            stdout = ""
            stderr = "ERROR: The system cannot find the file specified."

        return Result()

    monkeypatch.setattr(priv.subprocess, "run", fake_run)
    with pytest.raises(priv.HelperError) as excinfo:
        priv.run_via_helper(["cmd", "/c", "echo hi"])
    assert "Could not start the privileged helper" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Wiring: privileged writes go through the helper when it is installed
# ---------------------------------------------------------------------------
def test_write_routing_skips_the_helper_when_already_elevated(monkeypatch):
    """No point paying for a helper round trip when we are already privileged."""
    from xpshelper.service import make_service

    service = make_service("mock")
    monkeypatch.setattr("xpshelper.elevate.is_elevated", lambda: True)
    assert service._via_privileged_helper("charge", None) is False


def test_write_routing_skips_the_helper_when_not_installed(monkeypatch):
    from xpshelper.service import make_service

    service = make_service("mock")
    monkeypatch.setattr("xpshelper.elevate.is_elevated", lambda: False)
    monkeypatch.setattr(priv, "is_installed", lambda task_name=priv.TASK_NAME: False)
    assert service._via_privileged_helper("charge", None) is False


def test_write_routing_uses_the_helper_when_available(monkeypatch):
    """With the helper installed, a charge write is delegated -- no UAC prompt."""
    from xpshelper.model import ChargeConfig, ChargeMode
    from xpshelper.service import make_service

    service = make_service("mock")
    monkeypatch.setattr("xpshelper.elevate.is_elevated", lambda: False)
    monkeypatch.setattr(priv, "is_installed", lambda task_name=priv.TASK_NAME: True)

    called: dict = {}

    def fake_helper(command, **_kwargs):
        called["command"] = command
        return priv.HelperResult(exit_code=0)

    monkeypatch.setattr(priv, "run_via_helper", fake_helper)
    target = ChargeConfig(mode=ChargeMode.CUSTOM, start=60, stop=80)
    assert service._via_privileged_helper("charge", target) is True
    command = called["command"]
    assert "--start" in command and "60" in command
    assert "--stop" in command and "80" in command
    # The child applies the setting, so the parent must not verify/persist again.
    assert "--no-verify" in command
    # The child runs the lean internal command, not the public "charge" one: the
    # public command re-probes every BIOS attribute before writing, and a probe is
    # a multi-second firmware round trip the child does not need.
    assert "internal-charge" in command
    assert command[command.index("internal-charge") + 1] == "Custom"


def test_write_routing_never_recurses_into_the_helper(monkeypatch):
    """Regression guard for a real, shipped defect.

    The privileged child ran the same high-level command, decided it needed a
    privileged helper, and started another task -- which started another child, and
    so on. The symptom was a pile of hung processes and a 180 s timeout with no
    useful error.

    The guard must be an explicit marker, not an elevation *check*: a failed token
    query reads as "not elevated" and would let the recursion through. This test
    deliberately makes ``is_elevated()`` lie to prove the marker alone stops it.
    """
    from unittest import mock

    from xpshelper.service import make_service

    service = make_service("mock")
    monkeypatch.setenv(priv.ENV_PRIVILEGED_CHILD, "1")

    started: list = []

    def fake_helper(command, **_kwargs):  # pragma: no cover - must not be called
        started.append(command)
        return priv.HelperResult(exit_code=0)

    monkeypatch.setattr(priv, "is_installed", lambda task_name=priv.TASK_NAME: True)
    monkeypatch.setattr(priv, "run_via_helper", fake_helper)
    monkeypatch.setattr("xpshelper.elevate.is_elevated", lambda: False)

    assert service._via_privileged_helper("charge", None) is False
    assert started == [], "the privileged child must not start another helper"


def test_privileged_child_marker_is_read_from_the_environment(monkeypatch):
    monkeypatch.delenv(priv.ENV_PRIVILEGED_CHILD, raising=False)
    assert priv.is_privileged_child() is False
    monkeypatch.setenv(priv.ENV_PRIVILEGED_CHILD, "1")
    assert priv.is_privileged_child() is True
    monkeypatch.setenv(priv.ENV_PRIVILEGED_CHILD, "0")
    assert priv.is_privileged_child() is False


def test_run_via_helper_marks_the_child_as_privileged(monkeypatch):
    """The request must carry the marker, or the child would recurse."""
    import json as _json
    from pathlib import Path

    monkeypatch.setattr(priv, "is_installed", lambda task_name=priv.TASK_NAME: True)
    seen: dict = {}

    def fake_run(argv, **_kwargs):
        class Result:
            returncode = 0
            stdout = "ok"
            stderr = ""

        if argv[:2] == ["schtasks", "/run"]:
            request = _json.loads(Path(priv.request_path()).read_text(encoding="utf-8"))
            seen.update(request)
            Path(request["out_file"]).write_text("", encoding="utf-8")
            Path(request["err_file"]).write_text("", encoding="utf-8")
            priv.clear_request()
            Path(request["code_file"]).write_text("0\n", encoding="utf-8")
        return Result()

    monkeypatch.setattr(priv.subprocess, "run", fake_run)
    priv.run_via_helper(["cmd", "/c", "echo hi"])
    assert seen.get("env", {}).get(priv.ENV_PRIVILEGED_CHILD) == "1"


def test_install_script_carries_a_self_test_that_fails_the_install():
    """Registering a task that cannot run its own command is worse than none.

    The installed task looked fine while being unable to spawn anything, so the
    install now proves the task works and unregisters itself if it does not.
    """
    script = priv.install_script()
    assert "self-test  : PASSED" in script
    assert "has been removed" in script
    assert "exit 1" in script


def test_install_script_uses_interactive_logon():
    """S4U does not load a user profile, so a venv's python cannot bootstrap.

    The symptom was a task that reported "file not found" (Last Result 2) before
    Python ever ran.
    """
    script = priv.install_script()
    assert "LogonType Interactive" in script
    assert "LogonType S4U" not in script


def test_install_script_reports_its_own_failures():
    """A throwing cmdlet must not abort silently: the user saw a bare 'False'."""
    script = priv.install_script()
    assert "INSTALL FAILED" in script
    assert "catch" in script


def test_cli_exposes_privileged_run():
    """A general escape hatch for elevation-requiring diagnostics.

    Some probes need elevation to read at all (Dell's ACPI-WMI classes return
    "Access denied" otherwise). Re-launching elevated per probe means a UAC prompt
    each time; routing through the helper keeps it to the single install prompt.
    """
    from xpshelper.cli import build_parser

    args = build_parser().parse_args(["privileged-run", "--", "cmd", "/c", "echo hi"])
    assert args.command == "privileged-run"
    assert args.func.__name__ == "_cmd_privileged_run"
    assert args.command_args == ["--", "cmd", "/c", "echo hi"]


def test_privileged_run_rejects_an_empty_command(capsys):
    from xpshelper.cli import build_parser
    from xpshelper.service import make_service

    parser = build_parser()
    args = parser.parse_args(["privileged-run"])
    args.command_args = []
    code = args.func(args, make_service("mock"))
    assert code != 0
    assert "give a command" in capsys.readouterr().err


def test_privileged_run_reports_a_missing_helper(monkeypatch, capsys):
    from xpshelper.cli import build_parser
    from xpshelper.service import make_service

    monkeypatch.setattr(priv, "is_installed", lambda task_name=priv.TASK_NAME: False)
    args = build_parser().parse_args(["privileged-run", "--", "cmd", "/c", "echo hi"])
    code = args.func(args, make_service("mock"))
    assert code != 0
    assert "UAC prompt" in capsys.readouterr().err


def test_frozen_gui_delegates_to_the_console_executable(tmp_path, monkeypatch):
    """A packaged GUI must re-invoke the console build, not itself.

    Regression test for a real bug seen on a frozen build: ``xps-helper-gui.exe``
    is built with ``console=False`` and has no subcommands, so re-invoking itself
    produced

        xps-helper-gui: error: unrecognized arguments: internal-thermal

    and the thermal buttons in the window silently stopped working.
    """
    console = tmp_path / "xps-helper.exe"
    console.write_bytes(b"MZ")  # only existence is checked
    gui = tmp_path / "xps-helper-gui.exe"
    gui.write_bytes(b"MZ")

    monkeypatch.setattr(priv.sys, "frozen", True, raising=False)
    monkeypatch.setattr(priv.sys, "executable", str(gui), raising=False)

    argv = priv.self_invocation(["internal-thermal"])
    assert Path(argv[0]).name == "xps-helper.exe"
    assert argv[1:] == ["internal-thermal"]


def test_frozen_console_executable_uses_itself(tmp_path, monkeypatch):
    console = tmp_path / "xps-helper.exe"
    console.write_bytes(b"MZ")
    monkeypatch.setattr(priv.sys, "frozen", True, raising=False)
    monkeypatch.setattr(priv.sys, "executable", str(console), raising=False)

    argv = priv.self_invocation(["internal-thermal"])
    assert Path(argv[0]).name == "xps-helper.exe"


def test_frozen_build_without_a_sibling_falls_back_to_itself(tmp_path, monkeypatch):
    """An unusual layout must still produce something runnable."""
    gui = tmp_path / "xps-helper-gui.exe"
    gui.write_bytes(b"MZ")
    monkeypatch.setattr(priv.sys, "frozen", True, raising=False)
    monkeypatch.setattr(priv.sys, "executable", str(gui), raising=False)

    argv = priv.self_invocation(["internal-thermal"])
    assert Path(argv[0]).name == "xps-helper-gui.exe"


def test_a_task_pointing_at_a_missing_target_is_treated_as_stale(
    tmp_path, monkeypatch
):
    """Portability guard.

    The task stores an absolute path. Once the app moves -- or on another machine,
    where the path belongs to whoever installed it -- the task can never start, and
    callers would otherwise just see a timeout. It must report as not installed so
    the user is told to reinstall.

    The parsed action path is stubbed rather than faked through ``subprocess``:
    the real parser looks for a Windows drive path, so a ``tmp_path`` on Linux is
    not recognised and the test would pass or fail for the wrong reason.
    """
    missing = tmp_path / "gone" / "xps-helper.exe"
    assert not missing.exists()

    monkeypatch.setattr(priv.sys, "platform", "win32")
    monkeypatch.setattr(priv, "_task_action_path", lambda task_name=priv.TASK_NAME: str(missing))
    assert priv.action_target_exists() is False


def test_a_task_pointing_at_a_real_target_is_treated_as_valid(
    tmp_path, monkeypatch
):
    present = tmp_path / "xps-helper.exe"
    present.write_bytes(b"MZ")

    monkeypatch.setattr(priv.sys, "platform", "win32")
    monkeypatch.setattr(priv, "_task_action_path", lambda task_name=priv.TASK_NAME: str(present))
    assert priv.action_target_exists() is True


def test_an_unreadable_task_action_is_treated_as_valid(monkeypatch):
    """A localised Windows may not yield a parseable path; do not force a reinstall."""

    class Ok:
        returncode = 0
        stdout = "Some localised field with no path at all\n"

    monkeypatch.setattr(priv.sys, "platform", "win32")
    monkeypatch.setattr(priv.subprocess, "run", lambda *a, **k: Ok())
    assert priv.action_target_exists() is True


    """Source checkout: interpreter, ``-m``, package, then args."""
    monkeypatch.delattr(priv.sys, "frozen", raising=False)
    argv = priv.self_invocation(["internal-thermal"])
    assert argv[1:] == ["-m", "xpshelper", "internal-thermal"]


def test_self_invocation_reenters_the_program_when_frozen(monkeypatch):
    """Frozen build: the executable handles ``-m xpshelper`` itself.

    Regression test for a real bug. The thermal helper was started as
    ``[sys.executable, "-c", snippet]``, which works from source but not once
    packaged -- ``sys.executable`` is then ``xps-helper.exe``, not Python, so
    ``-c`` means nothing and the snippet was parsed as a subcommand name. The
    failure was ``invalid choice: "import json, sys; ..."``.
    """
    monkeypatch.setattr(priv.sys, "frozen", True, raising=False)
    argv = priv.self_invocation(["internal-thermal"])
    assert "-c" not in argv
    assert argv[1:] == ["internal-thermal"]


def test_thermal_helper_child_is_a_real_subcommand(monkeypatch):
    """The child must be invoked by subcommand name, never by ``-c``."""
    from xpshelper.service import make_service

    service = make_service("mock")
    monkeypatch.setattr(priv, "is_installed", lambda task_name=priv.TASK_NAME: True)
    monkeypatch.delenv(priv.ENV_PRIVILEGED_CHILD, raising=False)
    monkeypatch.setattr("xpshelper.elevate.is_elevated", lambda: False)

    captured: dict = {}

    def fake_helper(command, **_kwargs):
        captured["command"] = list(command)
        return priv.HelperResult(
            exit_code=0,
            stdout='{"error": null, "supported_mask": 15, "current_mode": 1, '
                   '"fan_failure": 0}',
        )

    monkeypatch.setattr(priv, "run_via_helper", fake_helper)
    tables = service._thermal_via_helper(read_only=True)

    command = captured["command"]
    assert "-c" not in command, command
    assert "internal-thermal" in command, command
    assert tables.current_mode == 1


def test_write_routing_raises_when_the_helper_fails(monkeypatch):
    """A helper failure must surface, not be swallowed into a false success."""
    from xpshelper.errors import XpsHelperError
    from xpshelper.model import ChargeConfig, ChargeMode
    from xpshelper.service import make_service

    service = make_service("mock")
    monkeypatch.setattr("xpshelper.elevate.is_elevated", lambda: False)
    monkeypatch.setattr(priv, "is_installed", lambda task_name=priv.TASK_NAME: True)
    monkeypatch.delenv(priv.ENV_PRIVILEGED_CHILD, raising=False)
    monkeypatch.setattr(
        priv, "run_via_helper",
        lambda command, **kw: priv.HelperResult(exit_code=1, stderr="access denied"),
    )
    with pytest.raises(XpsHelperError) as excinfo:
        service._via_privileged_helper("charge", ChargeConfig(mode=ChargeMode.STANDARD))
    assert "access denied" in str(excinfo.value)


    """A helper failure must surface, not be swallowed into a false success."""
    from xpshelper.errors import XpsHelperError
    from xpshelper.model import ChargeConfig, ChargeMode
    from xpshelper.service import make_service

    service = make_service("mock")
    monkeypatch.setattr("xpshelper.elevate.is_elevated", lambda: False)
    monkeypatch.setattr(priv, "is_installed", lambda task_name=priv.TASK_NAME: True)
    monkeypatch.delenv(priv.ENV_PRIVILEGED_CHILD, raising=False)
    monkeypatch.setattr(
        priv, "run_via_helper",
        lambda command, **kw: priv.HelperResult(exit_code=1, stderr="access denied"),
    )
    with pytest.raises(XpsHelperError) as excinfo:
        service._via_privileged_helper("charge", ChargeConfig(mode=ChargeMode.STANDARD))
    assert "access denied" in str(excinfo.value)


    """A helper failure must surface, not be swallowed into a false success."""
    from xpshelper.errors import XpsHelperError
    from xpshelper.model import ChargeConfig, ChargeMode
    from xpshelper.service import make_service

    service = make_service("mock")
    monkeypatch.setattr("xpshelper.elevate.is_elevated", lambda: False)
    monkeypatch.setattr(priv, "is_installed", lambda task_name=priv.TASK_NAME: True)
    monkeypatch.setattr(
        priv, "run_via_helper",
        lambda command, **kw: priv.HelperResult(exit_code=1, stderr="access denied"),
    )
    with pytest.raises(XpsHelperError) as excinfo:
        service._via_privileged_helper(
            "charge", ChargeConfig(mode=ChargeMode.STANDARD)
        )
    assert "access denied" in str(excinfo.value)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only mechanism")
def test_install_reports_a_dismissed_prompt_instead_of_a_bare_false():
    """Regression guard for a real, misleading failure.

    Running `helper --install` and dismissing the UAC prompt printed only
    "installed: False" -- which looks like a silent bug. ``cmd /c`` returns 0 even
    when the command inside it fails, so the exit code cannot distinguish
    "elevated work succeeded" from "nothing ran". A completion marker written by
    the script itself does, and its absence must raise with an explanation.

    ``is_elevated`` is imported inside the function, so it is patched at its
    source module rather than on the consumer.
    """
    from unittest import mock

    from xpshelper import privileged as p
    from xpshelper.elevate import ElevationError

    with mock.patch("xpshelper.elevate.is_elevated", lambda: False), \
         mock.patch("xpshelper.elevate._shell_execute",
                    side_effect=ElevationError("elevation did not complete")):
        with pytest.raises(p.HelperError) as excinfo:
            p.run_elevated_once("Write-Output 'noop'")
    assert "did not complete" in str(excinfo.value)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only mechanism")
def test_install_requires_the_completion_marker():
    """With elevation 'succeeding' but the script never running, it must raise.

    This is the exact shape of the silent failure: ``_shell_execute`` returns
    normally, the output files are empty, and without a marker the caller would
    report success.
    """
    from unittest import mock

    from xpshelper import privileged as p

    with mock.patch("xpshelper.elevate.is_elevated", lambda: False), \
         mock.patch("xpshelper.elevate._shell_execute", lambda *a, **k: 0):
        with pytest.raises(p.HelperError) as excinfo:
            p.run_elevated_once("Write-Output 'noop'")
    message = str(excinfo.value)
    assert "NOT installed" in message or "did not complete" in message


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only mechanism")
def test_install_script_neutralises_quotes_in_the_task_name():
    """The task name is the only interpolated value, so its quotes must be doubled.

    Without doubling, a name containing a single quote would close the string
    literal early and the rest would be parsed as PowerShell.
    """
    hostile = "x' ; Remove-Item -Recurse ; '"
    script = priv.install_script(hostile)
    assert "''" in script, "single quotes must be doubled to stay inert"

    # The name must appear only inside a quoted literal, with every embedded
    # quote doubled. Check by extracting the quoted span after -TaskName.
    import re

    match = re.search(r"-TaskName '((?:[^']|'')*)'", script)
    assert match, "task name must be a single-quoted PowerShell literal"
    # Reversing the doubling must recover exactly the original name.
    assert match.group(1).replace("''", "'") == hostile
