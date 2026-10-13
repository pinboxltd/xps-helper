"""Logging setup.

Logs go to the per-user config directory as well as stderr, because the GUI has
no console: when something fails on real hardware, the log file is the only
evidence a user can hand over.
"""

from __future__ import annotations

import logging
import os
import sys

from . import constants as C

_CONFIGURED = False


def setup_logging(verbose: bool = False) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    level = logging.DEBUG if verbose or os.environ.get(C.ENV_DEBUG) else logging.INFO
    root = logging.getLogger("xpshelper")
    root.setLevel(level)
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    try:
        C.config_dir().mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(C.log_file(), encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:
        # A read-only or redirected APPDATA should not stop the app from running.
        pass

    _CONFIGURED = True
