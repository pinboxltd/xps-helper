# Build the distributable executables.
#
#   pwsh -File scripts/build_exe.ps1
#
# Produces dist/xps-helper/ containing:
#   xps-helper.exe       console build (CLI output and exit codes work)
#   xps-helper-gui.exe   windowed build (no console flash; used for autostart)
#
# IMPORTANT: only files under dist/ are runnable. PyInstaller also leaves a copy
# of the executable in build/<name>/, and that copy is a scratch artefact: it has
# no _internal/ directory beside it, so launching it fails with
#
#     Failed to load Python DLL ... LoadLibrary: The specified module could not be found.
#
# which looks like a broken build but only means the wrong file was run.
param(
    [switch]$SkipTests,
    [switch]$Clean
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    $python = Join-Path $root '.venv\Scripts\python.exe'
    if (-not (Test-Path $python)) {
        throw "No interpreter at $python. Create the venv first (see README)."
    }

    if (-not $SkipTests) {
        Write-Host '== tests ==' -ForegroundColor Cyan
        & $python -m pytest -q
        if ($LASTEXITCODE -ne 0) { throw 'Tests failed; not building.' }
    }

    if ($Clean) {
        Write-Host '== cleaning build/ and dist/ ==' -ForegroundColor Cyan
        Remove-Item (Join-Path $root 'build'), (Join-Path $root 'dist') -Recurse -Force -ErrorAction SilentlyContinue
    }

    Write-Host '== pyinstaller ==' -ForegroundColor Cyan
    & $python -m PyInstaller packaging\xpshelper.spec --noconfirm --clean
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }

    # Drop a marker in the scratch directory so nobody tries to run it.
    $trap = Join-Path $root 'build\xpshelper'
    if (Test-Path $trap) {
        @'
These executables are PyInstaller build artefacts and DO NOT RUN.

They have no _internal/ directory beside them, so launching one fails with
"Failed to load Python DLL ... LoadLibrary: The specified module could not be found."

Run the copies in dist\xps-helper\ instead.
'@ | Set-Content (Join-Path $trap 'DO-NOT-RUN.txt') -Encoding utf8
    }

    Write-Host '== smoke test ==' -ForegroundColor Cyan
    $cli = Join-Path $root 'dist\xps-helper\xps-helper.exe'
    if (-not (Test-Path $cli)) { throw "No executable at $cli" }
    if (-not (Test-Path (Join-Path $root 'dist\xps-helper\_internal'))) {
        throw 'dist build is missing _internal/ and will not run.'
    }
    & $cli --version
    if ($LASTEXITCODE -ne 0) { throw 'The built executable does not run.' }

    Write-Host ''
    Write-Host 'Built and verified:' -ForegroundColor Green
    Get-ChildItem (Join-Path $root 'dist\xps-helper') -Filter *.exe |
        ForEach-Object { Write-Host ("  {0}  ({1:N1} MB)" -f $_.Name, ($_.Length / 1MB)) }
    Write-Host ''
    Write-Host 'Run dist\xps-helper\xps-helper.exe (CLI) or xps-helper-gui.exe (window).' -ForegroundColor Green
}
finally {
    Pop-Location
}
