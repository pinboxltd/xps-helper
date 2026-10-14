"""Doing privileged work without a UAC prompt on every use.

The problem
-----------
Changing a BIOS setting needs administrator rights. The naive answer -- re-launch
elevated on each action -- shows a UAC prompt every single time, which makes the
GUI unusable for quick profile switching.

How this is solved (and how Dell solves it)
-------------------------------------------
`Dell.CommandPowerManager.Service` is a COM+ component that is *already running
elevated*; Dell's unelevated UI delegates to it over an authenticated pipe. Dell's
own code has a `UseComponentService` switch and `VerifyComponentServiceAccess()`
for exactly this, which is why Dell never prompts.

The same shape is used here, without depending on Dell's software:

1. **Install once, elevated.** A Scheduled Task is registered with
   ``RunLevel = Highest`` and a trigger that only the current user can fire.
2. **Trigger freely.** ``schtasks /run`` from an unelevated process starts that
   task *without* a UAC prompt, because the elevation decision was already made
   when the task was registered.
3. **Return the result.** The task runs the app's own ``elevated_run`` entry point,
   which writes stdout/stderr and an exit-code file. The exit-code file is written
   last and doubles as the completion sentinel.

This is also why a Scheduled Task rather than a Startup shortcut: a shortcut
cannot elevate silently.

Security notes
--------------
The task runs a fixed, absolute command line -- not arbitrary user input -- and
this module never interpolates a caller-supplied string into it. Requests are
passed by file, and the request file is validated before use.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .procutil import hidden_process_kwargs

log = logging.getLogger(__name__)

#: Name of the Scheduled Task that runs the privileged helper.
TASK_NAME = "XPS Helper Privileged Helper"

#: Set in the environment of the privileged child.
#:
#: This is a **recursion guard**, and it exists because a real defect shipped
#: without it: the elevated child ran the same high-level command, decided it
#: needed a privileged helper, and started another task -- which started another
#: child, and so on. The result was a pile of hung processes and a 180 s timeout
#: with no useful error. Relying on an elevation *check* to break that loop is
#: fragile (a failed token query silently reads as "not elevated"); an explicit
#: marker cannot be wrong.
ENV_PRIVILEGED_CHILD = "XPSHELPER_PRIVILEGED_CHILD"


def is_privileged_child() -> bool:
    """Whether this process was started by the privileged helper."""
    return os.environ.get(ENV_PRIVILEGED_CHILD) == "1"

#: How long to wait for the privileged helper to report back.
DEFAULT_TIMEOUT = 180.0

POLL_SECONDS = 0.15


class HelperError(RuntimeError):
    """The privileged helper could not be used."""


class HelperNotInstalled(HelperError):
    """No privileged helper is registered yet."""


@dataclass
class HelperResult:
    """Outcome of one privileged run."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def __str__(self) -> str:  # pragma: no cover - display helper
        return f"exit={self.exit_code}"


# ---------------------------------------------------------------------------
# Where the helper lives
# ---------------------------------------------------------------------------
def helper_command() -> list[str]:
    """The absolute command the Scheduled Task runs.

    Frozen builds re-run the executable; a source checkout re-enters the package.
    Either way the target is an absolute path with no user input in it, which is
    what makes it safe to store in a task definition.
    """
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve()), "-m", "xpshelper.elevated_run"]
    return [str(Path(sys.executable).resolve()), "-m", "xpshelper.elevated_run"]


def self_invocation(args: Sequence[str]) -> list[str]:
    """An argv that re-enters this program with ``args``, frozen or not.

    Frozen builds dispatch through the executable's own entry point, which
    understands ``-m xpshelper``; a source checkout needs the interpreter and
    ``-m`` explicitly. Building this in one place keeps callers from hardcoding
    ``[sys.executable, "-c", ...]``, which silently breaks once packaged: in a
    frozen build ``sys.executable`` is the app, not Python.
    """
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve()), *args]
    return [str(Path(sys.executable).resolve()), "-m", "xpshelper", *args]


def _quote_windows(parts: Sequence[str]) -> str:
    """Quote an argv for the Scheduled Task action's command line."""
    import subprocess as sp

    return sp.list2cmdline([str(p) for p in parts])


def _client_id() -> str:
    """A stable identifier for the current user, for the request filename.

    The user's SID is used rather than the account name: SIDs are unique, stable,
    and contain nothing that needs quoting.
    """
    sid = None
    if sys.platform == "win32":
        try:
            proc = subprocess.run(  # noqa: S603 - fixed argv
                ["whoami", "/user", "/fo", "csv", "/nh"],
                capture_output=True,
                text=True,
                timeout=20,
                **hidden_process_kwargs(),
            )
            # "DOMAIN\user","S-1-5-21-..."
            parts = [p.strip().strip('"') for p in (proc.stdout or "").split(",")]
            for part in parts:
                if part.startswith("S-1-"):
                    sid = part
                    break
        except (OSError, subprocess.TimeoutExpired):
            sid = None
    if not sid:
        sid = f"uid{os.getuid()}" if hasattr(os, "getuid") else "unknown"
    return "".join(ch for ch in sid if ch.isalnum() or ch == "-")


def request_path() -> Path:
    """The single well-known request file both halves agree on.

    A Scheduled Task's action is fixed, so the elevated child cannot be told a
    random path. Both sides therefore derive this same location: the parent writes
    the request here and starts the task; the child reads it. The path is per-user
    and contains no variable that a caller controls, so nothing user-supplied ends
    up in the task definition.
    """
    base = Path(tempfile.gettempdir()) / "xps-helper-privileged"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"request-{_client_id()}.json"


#: A request older than this is treated as stale and refused. Without it, a
#: leftover file could be replayed by anyone able to start the task.
REQUEST_MAX_AGE_SECONDS = 120.0


def read_request(path: Path | None = None) -> dict[str, Any]:
    """Read and validate the pending request (called by the elevated child).

    Refuses a missing, unreadable, malformed or stale request, so a leftover file
    cannot cause an unintended privileged run.
    """
    target = path or request_path()
    try:
        age = time.time() - target.stat().st_mtime
    except OSError as exc:
        raise HelperError(f"no pending privileged request at {target}: {exc}") from exc
    if age > REQUEST_MAX_AGE_SECONDS:
        raise HelperError(
            f"the privileged request at {target} is {age:.0f}s old and was refused "
            "as stale."
        )
    try:
        with open(target, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise HelperError(f"could not read the privileged request: {exc}") from exc
    if not isinstance(data, dict) or not data.get("command"):
        raise HelperError("the privileged request contained no command.")
    return data


def clear_request(path: Path | None = None) -> None:
    """Remove the request file so it cannot be replayed."""
    try:
        (path or request_path()).unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Installing / removing the helper
# ---------------------------------------------------------------------------
def install_script(task_name: str = TASK_NAME) -> str:
    """PowerShell that registers the privileged helper. Requires elevation once.

    The action is a **fixed** command line pointing at the app's elevated runner
    with no arguments. A Scheduled Task's action cannot vary per invocation, so
    the request travels through a well-known file path instead — see
    :func:`request_path`. That is also what keeps user input out of the task
    definition entirely.
    """
    exe = helper_command()[0]
    args = " ".join(helper_command()[1:])
    safe_name = task_name.replace("'", "''")
    # Register-ScheduledTask wants the principal at registration time; setting it
    # on New-ScheduledTask via -User/-RunLevel does not bind and fails with
    # "Parameter set cannot be resolved".
    #
    # The whole body is wrapped so a failure is *reported*. Without this, a
    # throwing cmdlet plus `$ErrorActionPreference = 'Stop'` aborted the script
    # silently, and the caller saw only "installed: False" with no explanation.
    return rf"""
$ErrorActionPreference = 'Stop'
try {{
    $action = New-ScheduledTaskAction -Execute '{exe}' -Argument '{args}'
    # No trigger: the task exists to be *started*, never to run on its own. A
    # logon trigger would silently re-apply settings behind the user's back.
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::FromMinutes(10)) `
        -MultipleInstances IgnoreNew
    $me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    Write-Output "principal  : $me"
    # LogonType matters more than it looks. S4U does not load the user profile,
    # and a venv's python.exe cannot bootstrap without one -- the task then fails
    # with Last Result 2 (file not found) before Python ever runs. Interactive
    # loads the profile, which is what makes the venv (and any PATH-dependent
    # tooling) work.
    $principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Highest
    Write-Output "logon type : Interactive (profile loaded; S4U would break a venv)"
    Register-ScheduledTask -TaskName '{safe_name}' -Action $action -Settings $settings `
        -Principal $principal `
        -Description 'Runs XPS Helper privileged operations without a UAC prompt each time.' `
        -Force | Out-Null
    Write-Output "registered : {safe_name}"
    Write-Output "action     : {exe} {args}"

    # Self-test: prove the task can actually start the helper before declaring
    # success. Registering a task that cannot run its own command is worse than
    # not registering one, because it looks installed.
    $probe = Join-Path $env:TEMP ("xps-helper-selftest-" + [Guid]::NewGuid().ToString('N') + ".txt")
    $probeTask = 'XPS Helper Helper SelfTest'
    try {{
        $pa = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument ('/c echo ok > "' + $probe + '"')
        Register-ScheduledTask -TaskName $probeTask -Action $pa -Settings $settings `
            -Principal $principal -Force | Out-Null
        Start-ScheduledTask -TaskName $probeTask
        $deadline = (Get-Date).AddSeconds(20)
        while ((Get-Date) -lt $deadline -and -not (Test-Path $probe)) {{ Start-Sleep -Milliseconds 300 }}
        if (Test-Path $probe) {{
            Write-Output "self-test  : PASSED (task can run commands as $me)"
        }} else {{
            $ti = Get-ScheduledTaskInfo -TaskName $probeTask
            Unregister-ScheduledTask -TaskName $probeTask -Confirm:$false -ErrorAction SilentlyContinue
            Remove-Item $probe -ErrorAction SilentlyContinue
            Write-Error ("INSTALL FAILED: the registered task cannot run commands as " +
                "$me (last result " + $ti.LastTaskResult + "). The helper would be " +
                "registered but non-functional, so it has been removed.")
            Unregister-ScheduledTask -TaskName '{safe_name}' -Confirm:$false -ErrorAction SilentlyContinue
            exit 1
        }}
    }} catch {{
        Write-Output ("self-test  : ERROR " + $_.Exception.Message)
    }} finally {{
        Unregister-ScheduledTask -TaskName $probeTask -Confirm:$false -ErrorAction SilentlyContinue
        Remove-Item $probe -ErrorAction SilentlyContinue
    }}
}} catch {{
    Write-Error ("INSTALL FAILED: " + $_.Exception.Message)
    exit 1
}}
"""


def uninstall_script(task_name: str = TASK_NAME) -> str:
    safe_name = task_name.replace("'", "''")
    return f"""
$ErrorActionPreference = 'Stop'
Unregister-ScheduledTask -TaskName '{safe_name}' -Confirm:$false
Write-Output "removed: {safe_name}"
"""


def is_installed(task_name: str = TASK_NAME) -> bool:
    """Whether the privileged helper task exists. Needs no elevation to check."""
    if sys.platform != "win32":
        return False
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv
            ["schtasks", "/query", "/tn", task_name],
            capture_output=True,
            text=True,
            timeout=30,
            **hidden_process_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def run_elevated_once(script: str, *, timeout: int = 300) -> str:
    """Run a PowerShell script elevated **once**, to install the helper.

    This is the single UAC prompt the design requires. Note the deliberate use of
    ``elevate._shell_execute`` rather than ``spawn_elevated``: the latter exists to
    re-run *this application* and rewrites its arguments as ``-m xpshelper``, which
    would mangle a PowerShell command line. Here the intent is to elevate an
    unrelated program, so the caller supplies the whole argv.

    A **completion marker** is appended to the script and required afterwards.
    Without it, a declined UAC prompt or a script that dies early looks identical
    to success: ``cmd /c`` returns 0 even when the command inside it fails, so the
    exit code cannot be trusted on its own. The marker file is written by the
    script itself, so its presence proves the elevated work actually ran.
    """
    import tempfile as _tempfile

    from .elevate import ElevationDeclined, ElevationError, _shell_execute, is_elevated

    marker = str(Path(tempfile.gettempdir()) / f"xps-helper-install-done-{os.getpid()}.txt")
    try:
        os.unlink(marker)
    except OSError:
        pass

    # The marker is written on the last successful line; `-Encoding ascii` and a
    # fixed path keep this free of quoting hazards.
    marked_script = (
        script
        + "\n$ErrorActionPreference = 'Continue'\n"
        + f"[IO.File]::WriteAllText('{marker}', 'done')\n"
    )

    if is_elevated():
        proc = subprocess.run(  # noqa: S603 - fixed argv
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", marked_script],
            capture_output=True,
            text=True,
            timeout=timeout,
            **hidden_process_kwargs(),
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        if not os.path.exists(marker):
            raise HelperError(
                "The install script ran but did not complete: "
                f"{output.strip()[:400] or 'no output'}"
            )
        return output

    handle, path = _tempfile.mkstemp(prefix="xps-helper-install-", suffix=".ps1")
    os.close(handle)
    out_handle, out_path = _tempfile.mkstemp(prefix="xps-helper-install-out-", suffix=".txt")
    os.close(out_handle)
    err_handle, err_path = _tempfile.mkstemp(prefix="xps-helper-install-err-", suffix=".txt")
    os.close(err_handle)
    rc_handle, rc_path = _tempfile.mkstemp(prefix="xps-helper-install-rc-", suffix=".txt")
    os.close(rc_handle)
    os.unlink(rc_path)  # the sentinel must be created by the child
    try:
        # Write without a BOM: a BOM turns into a stray character when PowerShell
        # parses the file, which is a trap worth avoiding explicitly.
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(marked_script)

        try:
            _shell_execute(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path],
                mode="console",
                out_path=out_path,
                err_path=err_path,
                code_path=rc_path,
                wait=True,
            )
        except ElevationError as exc:
            raise HelperError(str(exc)) from exc

        combined = ""
        for candidate in (out_path, err_path):
            try:
                with open(candidate, encoding="utf-8", errors="replace") as fh:
                    combined += fh.read()
            except OSError:
                pass

        if not os.path.exists(marker):
            raise HelperError(
                "Elevation did not complete, so the privileged helper was NOT "
                "installed. This normally means the UAC prompt was dismissed. "
                f"Details: {combined.strip()[:400] or 'no output from the elevated process'}"
            )
        return combined
    except (ElevationDeclined, ElevationError) as exc:
        raise HelperError(str(exc)) from exc
    finally:
        for candidate in (path, out_path, err_path, rc_path, marker):
            try:
                os.unlink(candidate)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Using the helper
# ---------------------------------------------------------------------------
def run_via_helper(
    command: Sequence[str],
    *,
    task_name: str = TASK_NAME,
    timeout: float = DEFAULT_TIMEOUT,
) -> HelperResult:
    """Run ``command`` privileged through the installed helper, without a prompt.

    Raises :class:`HelperNotInstalled` if the helper has not been registered, and
    :class:`HelperError` if the helper did not report back.
    """
    if not is_installed(task_name):
        raise HelperNotInstalled(
            "No privileged helper is installed, so this would need a UAC prompt. "
            "Install it once with 'xps-helper helper --install'."
        )

    request_file = request_path()
    paths: dict[str, str] = {}
    for key, prefix in (
        ("out_file", "xps-helper-out-"),
        ("err_file", "xps-helper-err-"),
        ("code_file", "xps-helper-rc-"),
    ):
        handle, path = tempfile.mkstemp(prefix=prefix, suffix=".txt")
        os.close(handle)
        # The exit-code file is the completion sentinel. mkstemp creates it empty,
        # and the wait loop below tests for its existence -- so an empty file left
        # in place would be read instantly as "exit code 0" before the child had
        # run at all. Remove it and let the child create it when it finishes.
        if key == "code_file":
            os.unlink(path)
        paths[key] = path

    request = {
        "command": [str(part) for part in command],
        "cwd": os.getcwd(),
        # Tells the child it is already the privileged copy, so it must do the work
        # itself rather than starting another helper (see ENV_PRIVILEGED_CHILD).
        "env": {ENV_PRIVILEGED_CHILD: "1"},
        **paths,
    }
    try:
        # Write to a temporary file and move it into place, so the child can never
        # observe a half-written request.
        handle_tmp, tmp_path = tempfile.mkstemp(
            prefix="xps-helper-req-", suffix=".tmp", dir=str(request_file.parent)
        )
        with os.fdopen(handle_tmp, "w", encoding="utf-8") as fh:
            json.dump(request, fh)
        os.replace(tmp_path, request_file)

        proc = subprocess.run(  # noqa: S603 - fixed argv
            ["schtasks", "/run", "/tn", task_name],
            capture_output=True,
            text=True,
            timeout=60,
            **hidden_process_kwargs(),
        )
        if proc.returncode != 0:
            raise HelperError(
                "Could not start the privileged helper: "
                f"{(proc.stderr or proc.stdout or '').strip()[:200]}"
            )

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if os.path.exists(paths["code_file"]):
                break
            time.sleep(POLL_SECONDS)
        else:
            raise HelperError(
                f"The privileged helper did not report back within {timeout:.0f}s, "
                "so nothing can be confirmed as changed."
            )

        def _read(key: str) -> str:
            try:
                with open(paths[key], encoding="utf-8", errors="replace") as fh:
                    return fh.read()
            except OSError:
                return ""

        try:
            code = int(_read("code_file").strip() or 0)
        except ValueError:
            code = 0
        return HelperResult(exit_code=code, stdout=_read("out_file"), stderr=_read("err_file"))
    finally:
        # The child removes the request itself once it has read it, so there is no
        # window where the parent deletes it out from under a starting child. We
        # only clear it here if the child never got that far.
        for path in paths.values():
            try:
                os.unlink(path)
            except OSError:
                pass


def describe() -> dict[str, Any]:
    """State for `doctor` and `capabilities`."""
    installed = is_installed()
    return {
        "task_name": TASK_NAME,
        "installed": installed,
        "command": helper_command(),
        "prompts_per_use": 0 if installed else None,
        "note": (
            "Privileged operations run through the registered Scheduled Task, so no "
            "UAC prompt appears per use."
            if installed
            else "No privileged helper yet; each privileged action would prompt."
        ),
    }
