"""PyInstaller entry point for the console build.

A thin launcher rather than pointing PyInstaller at the package itself, so the
frozen executable's entry is stable and obvious.
"""

from __future__ import annotations

from xpshelper.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
