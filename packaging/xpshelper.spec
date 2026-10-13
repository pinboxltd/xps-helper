# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: builds both the CLI and the tray GUI from one tree.

Usage (from the project root, on Windows, in a venv that has pyinstaller):

    pip install pyinstaller
    pyinstaller packaging/xpshelper.spec --noconfirm

Produces ``dist/xps-helper/`` containing ``xps-helper.exe`` (console) and
``xps-helper-gui.exe`` (windowed, used for autostart).

Note: the GUI is built with ``console=False`` so double-clicking it does not
flash a console window. The console build is kept separate so CLI output and
exit codes work normally, which matters for Task Scheduler and scripting.

Only ``--onedir`` is configured, not ``--onefile``: a one-file build unpacks
itself to a temp directory on every launch, which makes startup noticeably
slower -- the opposite of what this app is for.
"""

from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
SRC = ROOT / "src"

# pystray needs its platform backend named explicitly; PyInstaller cannot see it
# because it is imported dynamically at runtime.
HIDDEN_IMPORTS = [
    "pystray",
    "pystray._win32",
    "PIL.Image",
    "PIL.ImageDraw",
    "PIL.ImageTk",
]

# Trim the bundle: none of these are used, and some are pulled in by optional
# paths we do not exercise.
EXCLUDES = [
    "pytest",
    "numpy",
    "matplotlib",
    "scipy",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "IPython",
    "setuptools",
    "pip",
]


def analyse(entry: Path):
    return Analysis(
        [str(entry)],
        pathex=[str(SRC)],
        binaries=[],
        datas=[],
        hiddenimports=HIDDEN_IMPORTS,
        hookspath=[],
        hooksconfig={},
        runtime_hooks=[],
        excludes=EXCLUDES,
        noarchive=False,
    )


cli_analysis = analyse(ROOT / "packaging" / "entry_cli.py")
gui_analysis = analyse(ROOT / "packaging" / "entry_gui.py")

# PYZ(pure) is the form accepted by both PyInstaller 5 and 6.
cli_pyz = PYZ(cli_analysis.pure)
gui_pyz = PYZ(gui_analysis.pure)

cli_exe = EXE(
    cli_pyz,
    cli_analysis.scripts,
    [],
    exclude_binaries=True,
    name="xps-helper",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

gui_exe = EXE(
    gui_pyz,
    gui_analysis.scripts,
    [],
    exclude_binaries=True,
    name="xps-helper-gui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # no console flash when launched from the Start menu
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

COLLECT(
    cli_exe,
    cli_analysis.binaries,
    cli_analysis.datas,
    gui_exe,
    gui_analysis.binaries,
    gui_analysis.datas,
    strip=False,
    upx=False,
    name="xps-helper",
)
