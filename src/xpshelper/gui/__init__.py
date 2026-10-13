"""GUI package.

Importing this package must never create a window: the CLI imports it lazily,
and the test-suite imports the pure helper functions on a headless machine.
"""

from __future__ import annotations

__all__ = ["run_gui"]


def __getattr__(name: str):
    # Lazy re-export so that importing `xpshelper.gui` does not pull in tkinter.
    if name == "run_gui":
        from .app import run_gui

        return run_gui
    raise AttributeError(name)
