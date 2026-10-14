"""PyInstaller entry point for the console build.

A thin launcher rather than pointing PyInstaller at the package itself, so the
frozen executable's entry is stable and obvious.

It also understands ``-m xpshelper.elevated_run``: self-elevation re-enters the
program with that form, and a frozen executable has no ``-m`` of its own, so the
form has to be interpreted here. Without this the elevated child would silently
run the ordinary CLI instead of the runner it was asked for.
"""

from __future__ import annotations

import sys

if __name__ == "__main__":
    argv = sys.argv[1:]
    if len(argv) >= 2 and argv[0] == "-m":
        module, rest = argv[1], argv[2:]
        if module == "xpshelper.elevated_run":
            from xpshelper.elevated_run import main as elevated_main

            raise SystemExit(elevated_main(rest))
        if module == "xpshelper":
            from xpshelper.cli import main as cli_main

            raise SystemExit(cli_main(rest))
    from xpshelper.cli import main

    raise SystemExit(main())
