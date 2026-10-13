"""Subprocess helpers, mostly to stop console windows appearing.

XPS Helper shells out to PowerShell for WMI access. Called from a windowed app
(``xps-helper-gui.exe`` is built with ``console=False``), Windows gives each
child process its own console window -- which flashes on screen every single
time the app reads a sensor. That looks broken, so every PowerShell launch goes
through :func:`hidden_process_kwargs`.
"""

from __future__ import annotations

import subprocess
import sys

#: Windows process-creation flag: no console window for the child.
CREATE_NO_WINDOW = 0x08000000

#: ShowWindow argument meaning "hide".
SW_HIDE = 0


def hidden_process_kwargs() -> dict:
    """Return subprocess kwargs that suppress the child's console window.

    Empty on POSIX. On Windows, both ``creationflags`` and a ``STARTUPINFO`` with
    ``SW_HIDE`` are used: the flag is what actually prevents the console from
    being allocated, and the startup info covers the case where the parent
    already has a console (running `xps-helper` from a terminal).
    """
    if sys.platform != "win32":
        return {}

    kwargs: dict = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", CREATE_NO_WINDOW)}
    try:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = SW_HIDE
        kwargs["startupinfo"] = startupinfo
    except (AttributeError, TypeError):  # pragma: no cover - non-Windows
        pass
    return kwargs


def powershell_argv(script: str) -> list[str]:
    """The argv for running a PowerShell script non-interactively."""
    return [
        "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        script,
    ]
