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


def _detach_console() -> None:
    """Detach this process from its console, so the window disappears.

    ``FreeConsole`` rather than ``ShowWindow(SW_HIDE)``: hiding the window guesses
    at ownership, whereas detaching is exact. If this process owned the console (a
    double-click) the console is destroyed and the black window vanishes; if a
    shell started us, that shell keeps its window and only we let go. Either way
    nobody is left with a stray console behind the window.
    """
    try:
        import ctypes

        ctypes.windll.kernel32.FreeConsole()
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

    # No arguments: this is the window. Detach from the console first so a
    # double-click does not leave a black console sitting behind it.
    _detach_console()
    from xpshelper.gui.app import main as gui_main

    try:
        return gui_main([])
    except Exception as exc:  # noqa: BLE001 - a windowed failure must be visible
        print(f"XPS Helper could not start its window: {exc}", file=sys.stderr)
        print("Run 'xps-helper --help' for the command line.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
