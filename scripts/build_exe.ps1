# Build the single-file executable.
#
#   powershell -File scripts/build_exe.ps1
#
# Produces dist/xps-helper.exe -- one self-contained file, no folder and no zip.
# Double-clicking it opens the window; running it with arguments runs the CLI.
#
# A one-file build unpacks itself into a temp directory on every launch, so the
# first start costs a little longer than the old folder build. That is the price
# of shipping one file instead of a 45 MB folder, and it is recorded in the spec.
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

    Write-Host '== pyinstaller (one file) ==' -ForegroundColor Cyan
    & $python -m PyInstaller packaging\xpshelper_onefile.spec --noconfirm --clean
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }

    Write-Host '== smoke test ==' -ForegroundColor Cyan
    $exe = Join-Path $root 'dist\xps-helper.exe'
    if (-not (Test-Path $exe)) { throw "No executable at $exe" }

    # The executable now carries a requireAdministrator manifest, so it cannot be
    # started from an unelevated shell at all -- Windows declines to launch it and
    # there is nothing to catch. Check the manifest rather than pretending to run
    # it, and only do the runtime checks when this shell is already elevated.
    $bytes = [IO.File]::ReadAllBytes($exe)
    $text = [Text.Encoding]::UTF8.GetString($bytes)
    if (-not $text.Contains('requireAdministrator')) {
        throw 'The built executable does not request administrator rights.'
    }
    Write-Host '  manifest: requireAdministrator (present)'

    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    $elevated = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

    if ($elevated) {
        & $exe --version
        if ($LASTEXITCODE -ne 0) { throw 'The built executable does not run.' }

        # The privileged child is dispatched with -m, which a frozen exe must
        # interpret itself. With no pending request it fails by design, and it
        # writes that failure to stderr -- which `$ErrorActionPreference = 'Stop'`
        # would turn into a thrown NativeCommandError. Relax it for this probe.
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            & $exe -m xpshelper.elevated_run 2>&1 | Out-Null
            $dispatchCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $previous
        }
        if ($dispatchCode -eq 0) { throw '-m dispatch did not behave as a privileged child.' }
        Write-Host '  runtime: --version and -m dispatch both verified'
    }
    else {
        Write-Host '  runtime: SKIPPED -- this shell is not elevated, and the' -ForegroundColor Yellow
        Write-Host '           executable requires administrator rights to start.' -ForegroundColor Yellow
        Write-Host '           Launch it yourself, or run this script elevated.' -ForegroundColor Yellow
    }

    # The scratch copy in build/ is not runnable on its own in a onedir build; in
    # a onefile build there is no folder to confuse, but leave a note anyway in
    # case an old onedir build is inspected.
    $trap = Join-Path $root 'build'
    if (Test-Path $trap) {
        @'
Anything here is a PyInstaller build artefact, not the program.

Run dist\xps-helper.exe instead -- that is the single self-contained file.
'@ | Set-Content (Join-Path $trap 'DO-NOT-RUN.txt') -Encoding utf8
    }

    $size = [math]::Round((Get-Item $exe).Length / 1MB, 1)
    Write-Host ''
    Write-Host ("Built and verified: dist\xps-helper.exe  ({0:N1} MB)" -f $size) -ForegroundColor Green
    Write-Host ''
    Write-Host 'Double-click it for the window, or run it with arguments for the CLI.' -ForegroundColor Green
}
finally {
    Pop-Location
}
