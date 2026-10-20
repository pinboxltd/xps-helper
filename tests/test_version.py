"""The declared version must match the package metadata.

This exists because it silently did not. The v0.7.1 and v0.7.2 releases were built
from source whose ``APP_VERSION`` still said **0.7.0**: the bump script replaced
``0.7.1`` with ``0.7.2``, there was no ``0.7.1`` to replace, and it wrote the file
anyway. Nothing failed, and the published executables reported a version two
releases behind their own tag.

A release whose ``--version`` disagrees with its tag is worse than a wrong number:
it makes every bug report ambiguous. One test is enough to stop that.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _app_version() -> str:
    text = (ROOT / "src" / "xpshelper" / "constants.py").read_text(encoding="utf-8")
    match = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', text)
    assert match is not None, "APP_VERSION not found in constants.py"
    return match.group(1)


def _pyproject_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match is not None, "version not found in pyproject.toml"
    return match.group(1)


def test_app_version_matches_pyproject():
    assert _app_version() == _pyproject_version(), (
        "APP_VERSION and pyproject.toml disagree; a release built now would "
        "report the wrong version"
    )


def test_version_looks_like_a_release_version():
    version = _app_version()
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), version
