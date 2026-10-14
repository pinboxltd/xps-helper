# End-to-end CLI write verification, run elevated.
#
# Proves the *shipped application* (not a hand-written probe) can change a real
# BIOS setting on this laptop, and that it reads the value back afterwards.
# Restores the original configuration at the end.
param([string]$OutFile = "$env:TEMP\xpshelper-cli-write.txt")

$ErrorActionPreference = 'Continue'
$exe = Join-Path $PSScriptRoot '..\.venv\Scripts\xps-helper.exe'
$exe = (Resolve-Path $exe).Path
$lines = @()
function W { param([string]$T) Write-Host $T; $script:lines += $T }

function Run-Xps {
    param([string[]]$CliArgs)
    $output = & $exe @CliArgs 2>&1 | Out-String
    $rc = $LASTEXITCODE
    return @{ rc = $rc; out = $output.Trim() }
}

W "elevated : $(([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))"
W "exe      : $exe"
W ""

W "=== 1. charge --list (no elevation needed, but shows real capability) ==="
$r = Run-Xps @('charge', '--list'); W $r.out
W ""

W "=== 2. capture the original charge setting ==="
$before = Run-Xps @('charge', '--json')
W $before.out
$original = $before.out | ConvertFrom-Json
W ""

W "=== 3. set Primarily AC Use (a real write) ==="
$r = Run-Xps @('charge', 'primarily-ac')
W ("exit code: {0}" -f $r.rc)
W $r.out
W ""

W "=== 4. read it back through a fresh process ==="
$r = Run-Xps @('charge', '--json'); W $r.out
W ""

W "=== 5. set Custom 60-80 (the three-attribute path) ==="
$r = Run-Xps @('charge', 'custom', '--start', '60', '--stop', '80')
W ("exit code: {0}" -f $r.rc)
W $r.out
W ""

W "=== 6. read custom back ==="
$r = Run-Xps @('charge', '--json'); W $r.out
W ""

W "=== 7. status (human output) ==="
$r = Run-Xps @('status'); W $r.out
W ""

W "=== 8. RESTORE the original setting ==="
if ($original -and $original.token) {
    W ("restoring token: {0}" -f $original.token)
    if ($original.mode -eq 'Custom') {
        $r = Run-Xps @('charge', 'custom', '--start', "$($original.start)", '--stop', "$($original.stop)")
    } else {
        $r = Run-Xps @('charge', $original.mode)
    }
    W ("exit code: {0}" -f $r.rc)
    W $r.out
} else {
    W "could not parse the original setting; setting Standard"
    W (Run-Xps @('charge', 'standard')).out
}
W ""

W "=== 9. final state ==="
W (Run-Xps @('charge', '--json')).out

$script:lines | Set-Content -Path $OutFile -Encoding UTF8
Write-Host "written $OutFile"
