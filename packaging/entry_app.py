"""PyInstaller entry point for the single-file build.

One executable serves three callers:

* no arguments -- open the window, which is what someone double-clicking a
  downloaded ``.exe`` expects;
* any arguments -- run the CLI, so scripts, shortcuts and ``--help`` work;
* ``-m xpshelper.elevated_run`` -- the privileged child, which is how the
  scheduled task invokes it. A frozen executable has no ``-m`` of its own, so the
  form has to be interpreted here.

Why "no arguments means the window" rather than trying to detect a double-click
-----------------------------------------------------------------------------
An earlier version tried to distinguish a double-click from a shell launch with
``GetConsoleProcessList``: a console created for the process has one member, a
shared one has more. That is the usual trick and it is not dependable -- it
answered "shared" even for launches that did create their own console, so the
program printed help and closed instead of opening a window. Guessing wrong is
far worse than being predictable, so the rule is now the simple one: no arguments
means the window, and the help is one flag away.
"""

from __future__ import annotations

import sys


def _attached_console_pids() -> list[int]:
    """PIDs attached to this process's console. Empty when there is none."""
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        buffer = (ctypes.c_uint * 32)()
        count = kernel32.GetConsoleProcessList(buffer, 32)
        return [int(buffer[index]) for index in range(max(0, count))]
    except Exception:  # noqa: BLE001
        return []


def _image_basename(pid: int) -> str | None:
    """Lowercased file name of a process's executable, or None if unknown."""
    try:
        import ctypes
        import os
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            size = wintypes.DWORD(1024)
            path = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
                return None
            return os.path.basename(path.value).lower()
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return None


def _console_is_ours() -> bool:
    """Whether every process attached to this console is this executable.

    Counting attached processes is not enough to decide. A one-file build is two
    processes (bootloader parent, child), but other processes can be attached for
    unrelated reasons, so "two means ours" is a guess that already failed once.

    The exact question is: is anything attached that is not us? If a shell
    (cmd.exe, pwsh.exe, Windows Terminal) is attached, hiding the window would
    blank the user's terminal, so we leave it alone. If the only attached
    processes are this executable -- which is what a double-click produces -- the
    window belongs to the app and can go.
    """
    import os

    pids = _attached_console_pids()
    if not pids:
        return False
    own = os.path.basename(sys.executable).lower()
    for pid in pids:
        name = _image_basename(pid)
        if name is None or name != own:
            # Unknown or foreign: cannot vouch for it, so do not hide.
            return False
    return True


def _hide_own_console() -> None:
    """Hide the console window the launcher created, when it belongs to the app.

    Two earlier attempts failed for concrete reasons:

    * ``FreeConsole`` **detaches** a process from its console; it does not remove a
      window another process still owns. A one-file build's bootloader parent owns
      it, so detaching left the black window exactly as before.
    * ``ShowWindow(SW_HIDE)`` does hide it, but the first version only ran when
      ``GetConsoleProcessList`` reported a single attached process. A one-file
      launch has two, so the guard was false and nothing was ever hidden.

    Now the guard asks the exact question -- is anything attached that is not this
    executable? -- instead of guessing from a count, and ``XPSHELPER_HIDE_CONSOLE``
    can force it either way: ``1`` always hides, ``0`` never does. That override
    exists because this decision has been wrong twice and needs an escape hatch
    that does not require a new build.
    """
    import os

    override = os.environ.get("XPSHELPER_HIDE_CONSOLE", "").strip().lower()
    if override == "0":
        return
    force = override == "1"

    try:
        import ctypes
        import logging

        if not force and not _console_is_ours():
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        window = kernel32.GetConsoleWindow()
        if not window:
            return
        user32.ShowWindow(window, 0)  # SW_HIDE
        kernel32.FreeConsole()
        logging.getLogger(__name__).info("hid the console window owned by the app")
    except Exception:  # noqa: BLE001 - non-Windows, or no console at all
        pass


def main() -> int:
    argv = sys.argv[1:]

    # The privileged child, started by the scheduled task.
    if len(argv) >= 2 and argv[0] == "-m":
        module, rest = argv[1], argv[2:]
        if module == "xpshelper.elevated_run":
            from xpshelper.elevated_run import main as elevated_main

            return elevated_main(rest)
        if module == "xpshelper":
            from xpshelper.cli import main as cli_main

            return cli_main(rest)

    # Anything explicit is CLI work.
    if argv:
        from xpshelper.cli import main as cli_main

        return cli_main(argv)

    # No arguments: this is the window. Hide the console the launcher created so a
    # double-click does not leave a black window sitting behind the app.
    #
    # Guarded on being frozen: a development run is always started from a shell,
    # and hiding the user's terminal would be much worse than a stray window.
    if getattr(sys, "frozen", False):
        _hide_own_console()
    from xpshelper.gui.app import main as gui_main

    try:
        return gui_main([])
    except Exception as exc:  # noqa: BLE001 - a windowed failure must be visible
        print(f"XPS Helper could not start its window: {exc}", file=sys.stderr)
        print("Run 'xps-helper --help' for the command line.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
