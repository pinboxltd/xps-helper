"""PyInstaller entry point for the windowed (no console) build."""

from __future__ import annotations

from xpshelper.gui.app import main

if __name__ == "__main__":
    raise SystemExit(main())
