# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: one self-contained executable.

Produces ``dist/xps-helper.exe`` -- a single file, no folder, no zip.

    pip install pyinstaller
    pyinstaller packaging/xpshelper.spec --noconfirm

Why one file
------------
Shipping the ``--onedir`` output meant distributing a 45 MB folder (or a zip of
it) so the user had to unzip before running anything. A single file is one
download and one place to put it.

The tradeoff, recorded because it is real: a ``--onefile`` build unpacks itself
into a temporary directory on **every** launch, so startup costs a few hundred
milliseconds more than the folder build. For a settings app launched once and
left running, that is a fair trade for not having to unzip anything; for a program
invoked in a tight loop it would not be.

One executable, not two
-----------------------
Windows decides whether to allocate a console when the process starts, and that
cannot be changed afterwards. So a single binary has to be a **console** program;
a windowed one could never print CLI output. ``entry_app.py`` therefore chooses
what to run from how it was started, and hides its own console when it was a
double-click. See that module for why ``GetConsoleProcessList`` is involved.
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

analysis = Analysis(
    [str(ROOT / "packaging" / "entry_app.py")],
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

# PYZ(pure) is the form accepted by both PyInstaller 5 and 6.
pyz = PYZ(analysis.pure)

# One-file: binaries and data go *into* the executable, and there is no COLLECT
# step, which is what would otherwise produce a folder.
exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="xps-helper",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # Must be a console build: a windowed one cannot print CLI output, and
    # entry_app.py hides this console itself on a double-click.
    console=True,
    # Bake the elevation requirement into the executable's manifest.
    #
    # Nearly everything this program does needs administrator rights: writing a
    # BIOS attribute, setting the thermal mode over ACPI-WMI, and registering the
    # privileged helper all require it. Asking at launch is honest and
    # predictable -- Windows prompts once, the process runs elevated, and every
    # action afterwards works.
    #
    # The alternative (running unelevated and delegating to a scheduled task) is
    # still supported and is what an elevated child relies on, but making the user
    # discover the requirement from a failed write was the worse experience.
    #
    # requireAdministrator means the program cannot be started unelevated at all;
    # a UAC prompt appears on every launch. That is the deliberate cost.
    uac_admin=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
