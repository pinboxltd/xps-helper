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

``_owns_console`` and ``_hide_own_console`` remain, but only to tidy up: a console
program that opens a window should not leave a black console sitting behind it.
Getting *that* wrong is harmless.
"""

from __future__ import annotations

import sys


def _owns_console() -> bool:
    """Whether this process created its console, as opposed to sharing one.

    Used only to decide whether hiding the console is polite. ``GetConsoleProcessList``
    reports everyone attached; a console created for this process has one member.
    """
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        buffer = (ctypes.c_uint * 8)()
        count = kernel32.GetConsoleProcessList(buffer, 8)
        return count <= 1
    except Exception:  # noqa: BLE001 - non-Windows, or no console at all
        return False


def _hide_own_console() -> None:
    """Hide the console window belonging to this process."""
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        window = kernel32.GetConsoleWindow()
        if window:
            user32.ShowWindow(window, 0)  # SW_HIDE
    except Exception:  # noqa: BLE001
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

    # No arguments: this is the window.
    if _owns_console():
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
