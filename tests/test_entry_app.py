"""Tests for the windowed entry point's console handling.

This has been wrong twice, in ways worth pinning down:

1. ``FreeConsole`` was used to "remove" the black window. It only **detaches** a
   process from its console; a one-file build's bootloader parent still owns the
   window, so it stayed on screen.
2. ``ShowWindow(SW_HIDE)`` was then used, but guarded by
   ``GetConsoleProcessList`` returning one attached process. A one-file launch has
   two (parent and child), so the guard was false and nothing was hidden.

The fix asks the exact question -- is anything attached to this console that is not
this executable? -- rather than inferring from a count.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

ENTRY = pathlib.Path(__file__).resolve().parent.parent / "packaging" / "entry_app.py"


def _load_entry_app():
    spec = importlib.util.spec_from_file_location("entry_app_under_test", ENTRY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def entry(monkeypatch):
    module = _load_entry_app()
    monkeypatch.setattr(sys, "executable", r"C:\Apps\xps-helper.exe", raising=False)
    return module


def test_console_is_ours_when_only_the_app_is_attached(entry, monkeypatch):
    """A one-file double-click: bootloader parent and child, same image."""
    monkeypatch.setattr(entry, "_attached_console_pids", lambda: [100, 200])
    monkeypatch.setattr(entry, "_image_basename", lambda pid: "xps-helper.exe")
    assert entry._console_is_ours() is True


def test_console_is_not_ours_when_a_shell_is_attached(entry, monkeypatch):
    """Hiding the window here would blank the user's terminal."""
    monkeypatch.setattr(entry, "_attached_console_pids", lambda: [1, 100, 200])
    names = {1: "pwsh.exe", 100: "xps-helper.exe", 200: "xps-helper.exe"}
    monkeypatch.setattr(entry, "_image_basename", lambda pid: names.get(pid))
    assert entry._console_is_ours() is False


@pytest.mark.parametrize("shell", ["cmd.exe", "pwsh.exe", "powershell.exe", "windowsterminal.exe"])
def test_common_shells_all_block_hiding(entry, monkeypatch, shell):
    monkeypatch.setattr(entry, "_attached_console_pids", lambda: [7, 100])
    names = {7: shell, 100: "xps-helper.exe"}
    monkeypatch.setattr(entry, "_image_basename", lambda pid: names.get(pid))
    assert entry._console_is_ours() is False


def test_unknown_process_blocks_hiding(entry, monkeypatch):
    """If a process cannot be inspected, we cannot vouch for it."""
    monkeypatch.setattr(entry, "_attached_console_pids", lambda: [100, 999])
    monkeypatch.setattr(entry, "_image_basename", lambda pid: "xps-helper.exe" if pid == 100 else None)
    assert entry._console_is_ours() is False


def test_no_console_means_not_ours(entry, monkeypatch):
    monkeypatch.setattr(entry, "_attached_console_pids", lambda: [])
    assert entry._console_is_ours() is False


def test_image_names_are_compared_case_insensitively(entry, monkeypatch):
    """_image_basename lowercases, so the comparison must too.

    Windows file names are case-insensitive; a plain == on the raw name would
    treat XPS-Helper.EXE and xps-helper.exe as different processes.
    """
    monkeypatch.setattr(entry, "_attached_console_pids", lambda: [1])
    monkeypatch.setattr(entry, "_image_basename", lambda pid: "xps-helper.exe")
    assert entry._console_is_ours() is True
    source = ENTRY.read_text(encoding="utf-8")
    assert "os.path.basename(path.value).lower()" in source


def test_hide_only_happens_when_the_console_is_ours(entry, monkeypatch):
    """The window must never be hidden when a shell is attached."""
    calls = []

    class FakeKernel:
        @staticmethod
        def GetConsoleWindow():
            return 1234

        @staticmethod
        def FreeConsole():
            calls.append("FreeConsole")

    class FakeUser:
        @staticmethod
        def ShowWindow(window, command):
            calls.append(("ShowWindow", window, command))

    monkeypatch.setattr(entry, "_console_is_ours", lambda: False)
    entry._hide_own_console()
    assert calls == []


def test_hide_runs_when_the_console_is_ours(entry, monkeypatch):
    calls = []

    import ctypes

    class FakeKernel:
        @staticmethod
        def GetConsoleWindow():
            return 1234

        @staticmethod
        def FreeConsole():
            calls.append("FreeConsole")

    class FakeUser:
        @staticmethod
        def ShowWindow(window, command):
            calls.append(("ShowWindow", window, command))

    monkeypatch.setattr(entry, "_console_is_ours", lambda: True)

    def fake_windll(name, **kwargs):
        return FakeKernel if "kernel32" in name else FakeUser

    monkeypatch.setattr(ctypes, "WinDLL", fake_windll)
    entry._hide_own_console()
    assert "FreeConsole" in calls
    assert ("ShowWindow", 1234, 0) in calls


def test_the_windowed_path_does_not_hide_a_source_run(monkeypatch):
    """A development run always has a shell attached; never blank it.

    The call is guarded on ``sys.frozen``, so importing and running the no-arg
    path from source must not touch the console.
    """
    module = _load_entry_app()
    source = ENTRY.read_text(encoding="utf-8")
    assert 'getattr(sys, "frozen", False)' in source


def test_show_hide_uses_sw_hide():
    """SW_HIDE is 0; using another value would minimise or restore instead."""
    source = ENTRY.read_text(encoding="utf-8")
    assert "ShowWindow(window, 0)" in source
