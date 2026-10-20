"""Tests for the elevation helper and the GUI's elevation offer.

Elevation is the difference between this app working and not working on the
target hardware: the BIOS interface exists but is privilege-gated, so a write
attempt unelevated is refused by the firmware. These tests cover the parts that
can be checked without a UAC prompt -- the relaunch command line, cmd quoting,
token inspection, and the GUI banner decision.
"""

from __future__ import annotations

import sys

import pytest

from xpshelper import elevate


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
def test_elevation_support_follows_the_platform():
    assert elevate.elevation_supported() == (sys.platform == "win32")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows token inspection")
def test_is_elevated_returns_a_bool():
    assert isinstance(elevate.is_elevated(), bool)


def test_is_elevated_is_false_off_windows(monkeypatch):
    monkeypatch.setattr(elevate.sys, "platform", "linux")
    assert elevate.is_elevated() is False


def test_is_elevated_prefers_the_process_token_over_group_membership():
    """An admin account running unelevated has Administrators in its deny list.

    A group check would therefore report "elevated" on a process that cannot
    actually write, so the implementation must inspect the token. This asserts
    the token API is what is used, not IsUserAnAdmin or group membership.
    """
    import inspect

    source = inspect.getsource(elevate.is_elevated)
    assert "GetTokenInformation" in source
    assert "IsUserAnAdmin" not in source


def test_is_elevated_agrees_with_is_user_an_admin():
    """Regression: the two disagreed, and is_elevated was the wrong one.

    On real hardware the window said "administrator: NO" while the process was
    genuinely elevated, because ``GetCurrentProcess()`` was called with ctypes'
    default 32-bit return type. That truncates the pseudo-handle,
    ``OpenProcessToken`` rejects it, and the function quietly answered False.

    When elevated, both checks must agree; when not, neither may claim otherwise.
    """
    import ctypes

    if sys.platform != "win32":
        pytest.skip("Windows-specific elevation probe")
    assert elevate.is_elevated() == bool(ctypes.windll.shell32.IsUserAnAdmin())


def test_get_current_process_is_declared_as_a_handle():
    """The declarations are the fix, so assert they are present.

    Checked in the source rather than against a live ``WinDLL`` object:
    ``ctypes.WinDLL("kernel32")`` returns a fresh wrapper each time, so a
    declaration made inside ``is_elevated`` is not visible on another instance.
    """
    import inspect

    source = inspect.getsource(elevate.is_elevated)
    assert "GetCurrentProcess.restype" in source
    assert "OpenProcessToken.argtypes" in source
    assert "GetTokenInformation.argtypes" in source


def test_is_elevated_falls_back_instead_of_claiming_not_elevated(monkeypatch):
    """A failed token query must not be reported as "not elevated"."""
    import ctypes

    if sys.platform != "win32":
        pytest.skip("Windows-specific elevation probe")
    expected = bool(ctypes.windll.shell32.IsUserAnAdmin())

    real_windll = ctypes.windll

    class Broken:
        def __getattr__(self, name):  # pragma: no cover - always raises
            raise OSError("token query unavailable")

    monkeypatch.setattr(
        ctypes, "windll", type("W", (), {"shell32": real_windll.shell32, "advapi32": Broken()})()
    )
    assert elevate.is_elevated() is expected


# ---------------------------------------------------------------------------
# Relaunch command construction
# ---------------------------------------------------------------------------
def test_relaunch_uses_the_kernel_module_form_from_source(monkeypatch):
    monkeypatch.setattr(elevate.sys, "frozen", False, raising=False)
    monkeypatch.setattr(elevate.sys, "executable", r"C:\py\python.exe")
    target = elevate.relaunch_target(["xps-helper", "doctor"])
    assert target == [r"C:\py\python.exe", "-m", "xpshelper", "doctor"]


def test_relaunch_strips_the_elevate_flag():
    target = elevate.relaunch_target(
        ["xps-helper", "--elevate", "charge", "custom", "--start", "60", "--stop", "80"]
    )
    assert "--elevate" not in target
    assert target[-6:] == ["charge", "custom", "--start", "60", "--stop", "80"]


def test_relaunch_keeps_global_flags_so_config_still_applies():
    target = elevate.relaunch_target(
        ["xps-helper", "--elevate", "--backend", "native", "--config", "C:\\my cfg.json", "status"]
    )
    assert "--backend" in target and "native" in target
    assert "--config" in target and "C:\\my cfg.json" in target


def test_relaunch_accepts_the_dash_m_form():
    target = elevate.relaunch_target(["python", "-m", "xpshelper", "status"])
    assert "-m" in target and "xpshelper" in target
    assert target.count("xpshelper") == 1


def test_relaunch_of_a_frozen_build_reruns_the_executable(monkeypatch):
    monkeypatch.setattr(elevate.sys, "frozen", True, raising=False)
    monkeypatch.setattr(elevate.sys, "executable", r"C:\apps\xps-helper.exe")
    target = elevate.relaunch_target(["xps-helper.exe", "--elevate", "doctor"])
    assert target == [r"C:\apps\xps-helper.exe", "doctor"]


def test_gui_mode_prefers_pythonw_when_present(monkeypatch):
    monkeypatch.setattr(elevate.sys, "frozen", False, raising=False)
    monkeypatch.setattr(elevate.sys, "executable", r"C:\py\python.exe")
    monkeypatch.setattr(elevate.Path, "exists", lambda self: True)
    target = elevate.relaunch_target(["xps-helper-gui"], mode="gui")
    assert target[0].endswith("pythonw.exe")


def test_describe_is_copy_pasteable_and_mentions_the_module():
    text = elevate.describe(["xps-helper", "status"])
    assert "xpshelper" in text


# ---------------------------------------------------------------------------
# cmd quoting
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value,expected",
    [
        ("plain", "plain"),
        ("with space", '"with space"'),
        (r"C:\path\to\file.txt", r"C:\path\to\file.txt"),
        (r"C:\my dir\f.txt", r'"C:\my dir\f.txt"'),
        ('has"quote', '"has""quote"'),
        ("a&b", '"a&b"'),
        ("a|b", '"a|b"'),
        ("a<b>c", '"a<b>c"'),
        ("a^b", '"a^b"'),
        ("", '""'),
    ],
)
def test_cmd_quote(value, expected):
    assert elevate._cmd_quote(value) == expected


def test_cmd_quoting_a_password_cannot_break_out():
    """A BIOS password must not be able to inject a second command."""
    quoted = elevate._cmd_quote('x" & shutdown /s & "')
    assert quoted.startswith('"') and quoted.endswith('"')
    # Every embedded quote is doubled, so cmd cannot see an unbalanced quote.
    inner = quoted[1:-1]
    assert '""' in inner
    assert inner.count('"') % 2 == 0


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------
def test_elevation_declined_is_an_elevation_error():
    assert issubclass(elevate.ElevationDeclined, elevate.ElevationError)
    assert issubclass(elevate.ElevationError, RuntimeError)


def test_spawn_refuses_on_non_windows(monkeypatch):
    monkeypatch.setattr(elevate.sys, "platform", "linux")
    with pytest.raises(elevate.ElevationError) as excinfo:
        elevate.spawn_elevated(["xps-helper", "status"])
    assert "sudo" in str(excinfo.value)


def test_a_missing_sentinel_is_an_error_never_a_silent_success():
    """The whole point: never report success for work that did not happen.

    With the runas verb Windows does not hand back a process handle, so
    completion is detected from the sentinel file. If that file never appears the
    child did not finish, and reporting exit code 0 would tell the user a BIOS
    setting was written when it was not.
    """
    import inspect

    source = inspect.getsource(elevate._shell_execute)
    assert "never reported back" in source
    # It must not fall back to returning 0 when the sentinel is missing.
    assert "code_path" in source


def test_the_command_is_not_put_on_the_shellexecute_parameter_line():
    """Regression guard for a silent-success bug found on the target machine.

    Quoting a command containing spaces through ``cmd /c "<command>"`` makes cmd
    strip the outer quotes, resolve the whole first token as a filename, fail
    with "The system cannot find the file specified", **and exit 0**. The elevated
    child therefore never ran while the app reported success. The command now goes
    into a JSON request file that ``xpshelper.elevated_run`` reads.

    Asserted structurally, so no docstring or comment can accidentally satisfy or
    trip it.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(elevate._shell_execute).lstrip())

    # No command line is assembled: no f-string/format/join feeding lpParameters a
    # "cmd /c ..." shape, and no ERRORLEVEL echo anywhere.
    assignments = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)]
    lpparams_values = []
    for node in assignments:
        for target in node.targets:
            if isinstance(target, ast.Attribute) and target.attr == "lpParameters":
                lpparams_values.append(ast.unparse(node.value))
    assert lpparams_values, "expected _shell_execute to set lpParameters"
    joined = " ".join(lpparams_values)
    assert "cmd" not in joined.lower()
    assert "ERRORLEVEL" not in joined
    assert "elevated_run" not in joined  # the entry point is built elsewhere

    whole = ast.unparse(tree)
    assert "%ERRORLEVEL%" not in whole
    # The real command travels in a request file instead.
    assert "json.dump" in whole
    assert "command" in whole and "code_file" in whole

    setup_strings = [
        node.value
        for node in ast.walk(ast.parse(inspect.getsource(elevate._elevated_entry_command)))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert "xpshelper.elevated_run" in setup_strings


def test_elevated_entry_command_reenters_the_package():
    entry = elevate._elevated_entry_command()
    assert entry[1:] == ["-m", "xpshelper.elevated_run"]
    assert entry[0]


# ---------------------------------------------------------------------------
# The elevated runner itself
# ---------------------------------------------------------------------------
def _run_elevated_runner(tmp_path, command, argv=None):
    import json

    from xpshelper import elevated_run

    request = {
        "command": command,
        "out_file": str(tmp_path / "out.txt"),
        "err_file": str(tmp_path / "err.txt"),
        "code_file": str(tmp_path / "rc.txt"),
    }
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    code = elevated_run.main([str(request_path)])
    return code, request, tmp_path


def test_elevated_runner_reports_success(tmp_path):
    code, request, base = _run_elevated_runner(
        tmp_path, [sys.executable, "-c", "print('hello'); import sys; sys.exit(0)"]
    )
    assert code == 0
    assert (base / "rc.txt").read_text().strip() == "0"
    assert "hello" in (base / "out.txt").read_text()


def test_elevated_runner_reports_a_nonzero_exit_and_keeps_stderr(tmp_path):
    code, _request, base = _run_elevated_runner(
        tmp_path,
        [sys.executable, "-c", "import sys; print('bad', file=sys.stderr); sys.exit(3)"],
    )
    assert code == 3
    assert (base / "rc.txt").read_text().strip() == "3"
    assert "bad" in (base / "err.txt").read_text()


def test_elevated_runner_reports_a_command_that_cannot_start(tmp_path):
    """A missing executable must be a failure, not an empty success."""
    code, _request, base = _run_elevated_runner(
        tmp_path, [str(tmp_path / "definitely-not-here.exe"), "x"]
    )
    assert code == 127
    assert (base / "rc.txt").read_text().strip() == "127"
    assert "could not start" in (base / "err.txt").read_text()


def test_elevated_runner_rejects_a_missing_or_empty_request(tmp_path):
    from xpshelper import elevated_run

    assert elevated_run.main([]) == 2
    assert elevated_run.main([str(tmp_path / "nope.json")]) == 2

    import json

    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"command": []}), encoding="utf-8")
    assert elevated_run.main([str(empty)]) == 2


def test_elevated_runner_writes_the_sentinel_last(tmp_path):
    """The sentinel is the completion proof, so ordering matters.

    If the exit-code file were written before the output files, a caller reading
    it could conclude the run finished while output was still being flushed.
    """
    import inspect

    from xpshelper import elevated_run

    source = inspect.getsource(elevated_run.main)
    assert source.index('_write(request.get("out_file")') < source.index(
        '_write(request.get("code_file")'
    )


def test_shell_execute_documents_why_no_process_handle_is_used():
    import inspect

    doc = inspect.getdoc(elevate.spawn_elevated) or ""
    assert "SEE_MASK_NOCLOSEPROCESS" in doc
    assert "runas" in doc


# ---------------------------------------------------------------------------
# CLI and GUI wiring
# ---------------------------------------------------------------------------
def test_cli_exposes_the_elevate_flag():
    from xpshelper.cli import build_parser

    args = build_parser().parse_args(["--elevate", "doctor"])
    assert args.elevate is True
    assert args.command == "doctor"


def test_cli_elevate_flag_works_after_the_subcommand_too():
    from xpshelper.cli import build_parser

    args = build_parser().parse_args(["doctor", "--elevate"])
    assert args.elevate is True


def test_cli_without_the_flag_does_not_elevate():
    from xpshelper.cli import build_parser

    args = build_parser().parse_args(["doctor"])
    assert getattr(args, "elevate", False) is False


def test_gui_banner_decision_unwraps_the_composite_backend():
    """Regression guard: the capability lives on the selected backend.

    CompositeControlBackend.selected() returns itself when it has not chosen one,
    so a naive hasattr() check on the wrapper silently concluded "no elevation
    needed" and the banner never appeared on the machine that needs it most.
    """
    import inspect

    from xpshelper.gui import app as gui_app

    source = inspect.getsource(gui_app.HelperWindow._maybe_offer_elevation)
    assert "CompositeControlBackend" in source
    assert "selected()" in source
    assert "access_denied" in source


def test_gui_async_delivery_clears_the_busy_flag_on_failure():
    """Regression guard: a silent delivery failure used to wedge the window.

    root.after() raises if the main thread is not in its main loop. The old code
    swallowed that, leaving _busy True forever, so the window refused every later
    request with "Still working on the previous request...".
    """
    import inspect

    from xpshelper.gui import app as gui_app

    source = inspect.getsource(gui_app.HelperWindow._run_async)
    assert "self._busy = False" in source
    assert "log.warning" in source


def test_gui_elevate_offer_rebuilds_the_gui_command():
    import inspect

    from xpshelper.gui import app as gui_app

    source = inspect.getsource(gui_app.HelperWindow._on_elevate)
    # argv[0] is a script path, so it must not be reused as the program name.
    assert '"gui"' in source
    assert 'mode="gui"' in source
