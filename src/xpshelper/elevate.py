"""Running ourselves with administrator rights.

Why this exists
---------------
On this hardware the firmware's BIOS-attribute interface is **present but
privilege-gated**: ``root\\dcim\\sysman\\biosattributes`` exists, yet an
unelevated process is refused with "Access denied", and every write fails until
the process runs as administrator. Verified on an XPS 13 7390 2-in-1 (BIOS
1.35.0) -- reading battery and sensors needs nothing, changing a BIOS setting
needs elevation.

Telling the user "run as administrator" and stopping there is a poor answer, so
the app can re-launch itself elevated via the standard Windows UAC prompt.

Three things this module deliberately takes care of:

1. **Never elevates silently.** Elevation always shows a UAC prompt; that is the
   security boundary and it is never bypassed. If the user declines, the caller
   gets :class:`ElevationDeclined` and can carry on unelevated.
2. **Passes the exact original argument vector through**, minus the flag that
   requested elevation, so ``--config`` and ``--backend`` still apply. The
   command shown in the UAC dialog is therefore the command that will run.
3. **Relaunches the kernel, not the script.** A frozen ``.exe`` re-runs itself; a
   source checkout is re-entered as ``python -m xpshelper``. Console apps keep
   their console, and a GUI launch uses ``pythonw`` so no black window appears.
"""

from __future__ import annotations

import os
import re
import shlex
import sys
from pathlib import Path
from typing import Sequence

#: Flag that asks the CLI to elevate before doing any work.
ELEVATE_FLAG = "--elevate"


class ElevationError(RuntimeError):
    """Elevation could not be started on this platform."""


class ElevationDeclined(ElevationError):
    """The user dismissed the UAC prompt, or it was cancelled."""

    def __init__(self, message: str = "Elevation was declined.") -> None:
        super().__init__(message)


def elevation_supported() -> bool:
    """Whether self-elevation is possible here (Windows only)."""
    return sys.platform == "win32"


def is_elevated() -> bool:
    """Whether the current process already runs with an elevated token.

    Uses the process token rather than group membership on purpose: an
    administrator account running unelevated has Administrators in its *deny*
    list, so a group check would wrongly report "elevated" and the app would
    happily attempt writes that the firmware then refuses.

    Every Win32 call below declares its argument and return types. That is not
    decoration: without them ctypes treats a handle as a 32-bit ``int``, and
    ``GetCurrentProcess()`` is a pseudo-handle that does not survive the
    truncation. ``OpenProcessToken`` is then handed a value it rejects, and this
    function returned False **even when the process was genuinely elevated** --
    which is exactly the bug that made the window report "administrator: NO"
    while the app was running elevated.
    """
    if not elevation_supported():
        return False
    try:
        import ctypes
        from ctypes import wintypes

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        kernel32.GetCurrentProcess.restype = wintypes.HANDLE

        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        advapi32.OpenProcessToken.restype = wintypes.BOOL

        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,          # TOKEN_INFORMATION_CLASS
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.GetTokenInformation.restype = wintypes.BOOL

        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        TOKEN_QUERY = 0x0008
        TOKEN_ELEVATION = 20  # a DWORD that is non-zero when elevated

        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)
        ):
            return _elevated_fallback()

        try:
            elevated = wintypes.DWORD()
            returned = wintypes.DWORD()
            ok = advapi32.GetTokenInformation(
                token,
                TOKEN_ELEVATION,
                ctypes.byref(elevated),
                ctypes.sizeof(elevated),
                ctypes.byref(returned),
            )
            if not ok:
                return _elevated_fallback()
            return bool(elevated.value)
        finally:
            kernel32.CloseHandle(token)
    except Exception:  # noqa: BLE001 - never let a probe break the app
        return _elevated_fallback()


def _elevated_fallback() -> bool:
    """Second opinion when the token query cannot be completed.

    ``IsUserAnAdmin`` is less precise than TOKEN_ELEVATION -- it answers "is this
    token an administrator" -- but it is better than claiming "not elevated" on a
    machine where the token query failed for an unrelated reason. Getting this
    wrong in the other direction would make the app skip its privileged path when
    it actually needs it, so a false negative is the safer failure; this only
    helps when the primary check is unavailable rather than merely negative.
    """
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def _python_for(mode: str) -> str:
    """The interpreter to relaunch with, preferring the windowed one for a GUI."""
    if mode == "gui":
        candidate = Path(sys.executable).with_name("pythonw.exe")
        if candidate.exists():
            return str(candidate)
    return sys.executable


def relaunch_target(argv: Sequence[str] | None = None, *, mode: str = "console") -> list[str]:
    """The command line that re-runs this application, as an argv list.

    Frozen builds re-run the executable; a source checkout is re-entered as
    ``-m xpshelper``. ``argv`` is the *full* original vector (including
    ``sys.argv[0]`` when called from :func:`main`).
    """
    if argv is None:
        argv = sys.argv

    if getattr(sys, "frozen", False):
        remainder = [arg for arg in argv[1:] if arg != ELEVATE_FLAG]
        return [sys.executable, *remainder]

    # Normalise whatever form the caller used down to the arguments after the
    # program name, so both ["-m", "xpshelper", ...] and ["xps-helper", ...] work.
    tail = list(argv[1:])
    if tail[:2] == ["-m", "xpshelper"]:
        tail = tail[2:]
    return [_python_for(mode), "-m", "xpshelper", *[a for a in tail if a != ELEVATE_FLAG]]


def spawn_elevated(
    argv: Sequence[str] | None = None,
    *,
    mode: str = "console",
    capture: bool = True,
    wait: bool = True,
) -> int:
    """Re-launch this application elevated, returning the child's exit code.

    Two Windows facts shape this function, both established by experiment on the
    target machine:

    1. ``SEE_MASK_NOCLOSEPROCESS`` is **not honoured for the ``runas`` verb** --
     ``ShellExecuteW`` always returns 42 and leaves ``hProcess`` NULL, because the
     elevated process is created by the AppInfo service, not by our process.
       So the child cannot be waited on through a handle.
    2. An elevated process gets a fresh console and does **not** inherit our
       stdout/stderr handles, so its output cannot be piped back.

    Both are solved the same way: ``cmd.exe /c`` wraps the real command, redirects
    its output to files, and writes an exit-code file *last*. That file is the
    completion sentinel -- it appears only after the child has finished, which is
    what makes "did this actually run, and did it work?" answerable without a
    process handle.

    ``wait=False`` is for replacing a GUI: the raised copy takes over and the
    caller then exits, so waiting would hold the old window open until the new one
    was closed.
    """
    if not elevation_supported():
        raise ElevationError(
            "Self-elevation is only implemented for Windows. On Linux, run the "
            "command with sudo instead."
        )

    import tempfile

    command = relaunch_target(argv, mode=mode)

    out_path = err_path = code_path = None
    if capture:
        out_path = tempfile.mktemp(prefix="xps-helper-out-", suffix=".txt")
        err_path = tempfile.mktemp(prefix="xps-helper-err-", suffix=".txt")
        code_path = tempfile.mktemp(prefix="xps-helper-rc-", suffix=".txt")

    try:
        code = _shell_execute(
            command,
            mode=mode,
            out_path=out_path,
            err_path=err_path,
            code_path=code_path,
            wait=wait,
        )
    except BaseException:
        _cleanup(out_path, err_path, code_path)
        raise

    if capture:
        _forward(out_path, sys.stdout)
        _forward(err_path, sys.stderr)
        _cleanup(out_path, err_path, code_path)

    return code


def _cleanup(*paths: str | None) -> None:
    for path in paths:
        if not path:
            continue
        try:
            os.unlink(path)
        except OSError:
            pass


def _forward(path: str | None, stream) -> None:
    """Echo a captured file to a stream, then stop caring about it."""
    if not path:
        return
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return
    if text:
        try:
            stream.write(text)
            stream.flush()
        except (ValueError, OSError):
            pass


def _cmd_quote(value: str) -> str:
    """Quote one token for a ``cmd.exe`` command line.

    ``subprocess.list2cmdline`` follows the MSVCRT rules, which are subtly wrong
    for cmd: cmd treats ``\\"`` as a literal backslash followed by a quote. Dell
    BIOS passwords and config paths can contain quotes, so this closes that hole.
    """
    text = str(value)
    if not text:
        return '""'
    if not re.search(r'[\s"&|<>^]', text):
        return text
    return '"' + text.replace('"', '""') + '"'


def _shell_execute(
    command: Sequence[str],
    *,
    mode: str,
    out_path: str | None = None,
    err_path: str | None = None,
    code_path: str | None = None,
    wait: bool = True,
) -> int:
    """Start ``command`` elevated, wait for its completion sentinel, return its code.

    The command is **not** placed on the ``ShellExecuteW`` parameter line. It goes
    into a JSON request file handed to :mod:`xpshelper.elevated_run`, which runs
    it inside the elevated session. See that module for why: a command containing
    spaces, quoted through ``cmd /c "..."``, makes cmd resolve the whole first
    token as a filename, fail, **and still exit 0** -- reporting success for work
    that never happened.
    """
    import ctypes
    import json
    import tempfile
    import time

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SW_SHOWNORMAL = 1
    SW_HIDE = 0
    # Generous: the child does real work (WMI round-trips take seconds), and a UAC
    # prompt may sit on screen until the user gets to it.
    TIMEOUT_SECONDS = 600
    POLL_SECONDS = 0.15

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_ulong),
            ("fMask", ctypes.c_ulong),
            ("hwnd", ctypes.c_void_p),
            ("lpVerb", ctypes.c_wchar_p),
            ("lpFile", ctypes.c_wchar_p),
            ("lpParameters", ctypes.c_wchar_p),
            ("lpDirectory", ctypes.c_wchar_p),
            ("nShow", ctypes.c_int),
            ("hInstApp", ctypes.c_void_p),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", ctypes.c_wchar_p),
            ("hkeyClass", ctypes.c_void_p),
            ("dwHotKey", ctypes.c_ulong),
            ("hIcon", ctypes.c_void_p),
            ("hProcess", ctypes.c_void_p),
        ]

    entry = _elevated_entry_command()
    request = {
        "command": list(command),
        "cwd": os.getcwd(),
        "out_file": out_path,
        "err_file": err_path,
        "code_file": code_path,
    }

    request_handle, request_path = tempfile.mkstemp(prefix="xps-helper-req-", suffix=".json")
    try:
        with os.fdopen(request_handle, "w", encoding="utf-8") as handle:
            json.dump(request, handle)

        info = SHELLEXECUTEINFOW()
        info.cbSize = ctypes.sizeof(info)
        info.fMask = SEE_MASK_NOCLOSEPROCESS
        info.lpVerb = "runas"
        info.lpFile = entry[0]
        # One space-free path plus one JSON path: nothing here can be mis-parsed.
        info.lpParameters = _quote_windows_command([*entry[1:], request_path])
        info.lpDirectory = os.getcwd()
        info.nShow = SW_SHOWNORMAL if mode == "gui" else SW_HIDE

        shell32 = ctypes.windll.shell32
        shell32.ShellExecuteW.restype = ctypes.c_void_p
        result = shell32.ShellExecuteW(
            None, "runas", info.lpFile, info.lpParameters, info.lpDirectory, info.nShow
        )
        if not result or result <= 32:
            raise ElevationDeclined(
                "Elevation was cancelled, so nothing was changed. Run the app from an "
                "already-elevated prompt if you would rather not see the prompt."
            )

        # The handle is not available with the runas verb, so wait on the sentinel
        # the child writes when it is done. If it never appears the process never
        # ran -- that must be an error, never a silent success.
        if not wait or not code_path:
            return 0

        deadline = time.monotonic() + TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if os.path.exists(code_path):
                break
            time.sleep(POLL_SECONDS)
        else:
            detail = ""
            for label, path in (("output", out_path), ("errors", err_path)):
                try:
                    with open(path, encoding="utf-8", errors="replace") as handle:
                        text = handle.read().strip()
                    if text:
                        detail += f"\n{label}: {text[:500]}"
                except OSError:
                    pass
            raise ElevationError(
                "The elevated process never reported back, so nothing can be confirmed "
                "as changed. The UAC prompt may have been dismissed, or the elevated "
                f"copy failed to start.{detail}"
            )

        try:
            with open(code_path, encoding="utf-8", errors="replace") as handle:
                return int(handle.read().strip() or 0)
        except (OSError, ValueError):
            # The sentinel exists but is unreadable; the child ran, so fall back to
            # "it ran and said nothing" rather than inventing a failure code.
            return 0
    finally:
        try:
            os.unlink(request_path)
        except OSError:
            pass


def _elevated_entry_command() -> list[str]:
    """Arguments that re-enter this package's elevated runner.

    Works for both a source checkout and a frozen build: ``sys.executable`` is the
    interpreter in the first case and the packaged executable in the second, and
    both understand ``-m xpshelper.elevated_run``.
    """
    return [sys.executable, "-m", "xpshelper.elevated_run"]


def _quote_windows_command(parts: Sequence[str]) -> str:
    """Join argv for a ``ShellExecuteW`` parameter line.

    ``subprocess.list2cmdline`` follows the MSVCRT rules that
    ``CommandLineToArgvW`` (and therefore CPython) parses, so paths containing
    spaces survive. This is only ever used for the launcher's own arguments now,
    never to build a whole ``cmd`` command line.
    """
    import subprocess

    return subprocess.list2cmdline([str(part) for part in parts])


def describe(argv: Sequence[str] | None = None, *, mode: str = "console") -> str:
    """A copy-pasteable command that does the same thing, elevated.

    Used in messages so the advice is exact rather than "run as administrator".
    """
    return shlex.join(relaunch_target(argv, mode=mode))
