"""Guards against the way the GUI broke: a duplicate method that silently won.

Clicking any charge button raised

    WARNING xpshelper.gui.widgets: mode button callback failed:
    'str' object has no attribute 'value'

because ``HelperWindow`` defined ``_select_charge_mode`` **twice** -- one taking a
string for the buttons, one taking a ``ChargeMode`` for the tray. Python keeps the
last definition, so the buttons reached the enum-only version and it called
``.value`` on a string.

Nothing caught it: both definitions were individually valid, the module compiled,
and the failing path only ran when someone clicked. These tests assert the shapes
that would have caught it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from xpshelper.gui import app as gui_app
from xpshelper.model import ChargeMode

APP_PATH = Path(gui_app.__file__)


def _class_methods() -> dict[str, int]:
    tree = ast.parse(APP_PATH.read_text(encoding="utf-8"))
    counts: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "HelperWindow":
            for item in node.body:
                if isinstance(item, ast.FunctionDef):
                    counts[item.name] = counts.get(item.name, 0) + 1
    return counts


def test_helper_window_defines_no_method_twice():
    """A duplicate silently shadows the earlier definition."""
    duplicates = {name: n for name, n in _class_methods().items() if n > 1}
    assert duplicates == {}, f"duplicate method definitions: {duplicates}"


def test_no_helper_window_method_is_left_without_a_body():
    """The same edit that duplicated a method also stripped bodies.

    A method whose only statement is a docstring is not a syntax error -- it just
    does nothing when called, which is worse than failing.
    """
    tree = ast.parse(APP_PATH.read_text(encoding="utf-8"))
    empty: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "HelperWindow":
            for item in node.body:
                if not isinstance(item, ast.FunctionDef):
                    continue
                body = [
                    statement
                    for statement in item.body
                    if not (
                        isinstance(statement, ast.Expr)
                        and isinstance(statement.value, ast.Constant)
                    )
                ]
                if not body:
                    empty.append(item.name)
    assert empty == [], f"methods with no statements: {empty}"


@pytest.mark.parametrize("mode", list(ChargeMode))
def test_select_charge_mode_accepts_the_strings_the_buttons_pass(mode):
    """The buttons pass ``mode.value``, a string, not the enum."""
    window = gui_app.HelperWindow.__new__(gui_app.HelperWindow)
    window._charge_var = _Var()
    window._charge_buttons = {}
    window._mark_selected = lambda *_a, **_k: None
    window._on_charge_mode_changed = lambda: None
    window._on_apply_charge = lambda: None

    window._select_charge_mode(mode.value)  # must not raise
    assert window._charge_var.value == mode.value


@pytest.mark.parametrize("mode", list(ChargeMode))
def test_select_charge_mode_accepts_the_enums_the_tray_passes(mode):
    window = gui_app.HelperWindow.__new__(gui_app.HelperWindow)
    window._charge_var = _Var()
    window._charge_buttons = {}
    window._mark_selected = lambda *_a, **_k: None
    window._on_charge_mode_changed = lambda: None
    window._on_apply_charge = lambda: None

    window._select_charge_mode(mode)  # must not raise
    assert window._charge_var.value == mode.value


def test_select_charge_mode_ignores_an_unknown_value():
    window = gui_app.HelperWindow.__new__(gui_app.HelperWindow)
    window._charge_var = _Var()
    window._charge_buttons = {}
    window._mark_selected = lambda *_a, **_k: None
    window._on_charge_mode_changed = lambda: None
    window._on_apply_charge = lambda: None

    window._select_charge_mode("NotAChargeMode")  # must not raise
    assert window._charge_var.value is None


class _Var:
    """Minimal stand-in for a tk StringVar."""

    def __init__(self) -> None:
        self.value = None

    def set(self, value) -> None:
        self.value = value

    def get(self):
        return self.value
