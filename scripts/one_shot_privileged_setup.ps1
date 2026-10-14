# One-shot privileged setup and verification for XPS Helper.
#
# Run this from a NORMAL (unelevated) PowerShell, once:
#
#     powershell -ExecutionPolicy Bypass -File .\scripts\one_shot_privileged_setup.ps1
#
# You will get exactly ONE UAC prompt. The script then:
#   1. removes the old, non-functional helper task (it used S4U logon, which does
#      not load a user profile, so the task could not spawn any program)
#   2. re-registers it with Interactive logon and self-tests it
#   3. proves a privileged action runs with NO further prompt
#   4. captures the UTT thermal buffer (read-only)
#   5. pauses at the end so you can copy the result back
#
# Nothing here changes a firmware setting. Step 4 only *reads* the thermal tables.

$ErrorActionPreference = 'Continue'
$d = "C:\Users\Stark-Laptop\Desktop\XPS-Helper"
$py = Join-Path $d '.venv\Scripts\python.exe'
$cli = Join-Path $d '.venv\Scripts\xps-helper.exe'
$env:PYTHONPATH = (Join-Path $d 'src')

# Re-launch ourselves elevated, once, and keep the window open.
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Requesting administrator rights (the ONE prompt)..." -ForegroundColor Yellow
    $args = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-NoExit', '-File', $PSCommandPath)
    Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $args
    return
}

$log = Join-Path $env:TEMP 'xps-helper-oneshot.txt'
Start-Transcript -Path $log -Force | Out-Null
function H($t) { Write-Host ""; Write-Host "=== $t ===" -ForegroundColor Cyan }

H "0. context"
Write-Host "elevated : $([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)"
Write-Host "user     : $([Security.Principal.WindowsIdentity]::GetCurrent().Name)"
Write-Host "python   : $py"
Write-Host "python exists: $(Test-Path $py)"

H "1. remove the old task (S4U-based, could not spawn programs)"
schtasks /delete /tn "XPS Helper Privileged Helper" /f 2>&1 | ForEach-Object { Write-Host "   $_" }

H "2. register the helper with Interactive logon + self-test"
$installOut = & $cli helper --install 2>&1
$installOut | ForEach-Object { Write-Host "   $_" }

H "3. task state"
schtasks /query /tn "XPS Helper Privileged Helper" /fo LIST /v 2>&1 |
    Select-String -Pattern "TaskName|Status|Logon Mode|Run As User|Last Result" |
    ForEach-Object { Write-Host "   $_" }

H "4. NO-UAC PROOF: privileged charge write with no --elevate (no prompt)"
$before = & $cli charge 2>&1 | Select-String 'Active:'
Write-Host "   before: $before"
$sw = [Diagnostics.Stopwatch]::StartNew()
$w1 = & $cli charge primarily-ac 2>&1 | Select-Object -Last 2
$sw.Stop()
Write-Host "   write 1 (primarily-ac): exit=$LASTEXITCODE in $([math]::Round($sw.Elapsed.TotalSeconds,1))s"
$w1 | ForEach-Object { Write-Host "      $_" }

$sw.Restart()
$w2 = & $cli charge custom --start 50 --stop 90 2>&1 | Select-Object -Last 2
$sw.Stop()
Write-Host "   write 2 (restore 50-90): exit=$LASTEXITCODE in $([math]::Round($sw.Elapsed.TotalSeconds,1))s"
$w2 | ForEach-Object { Write-Host "      $_" }

H "5. verify by reading the firmware back"
(& $cli charge 2>&1 | Select-String 'Active:') | ForEach-Object { Write-Host "   $_" }

H "6. capture the USTT thermal buffer (READ-ONLY)"
& (Join-Path $d 'scripts\capture_ustt_buffer.ps1') 2>&1 | Select-Object -First 40 | ForEach-Object { Write-Host "   $_" }

H "7. summary"
Write-Host "helper installed : $(& $cli helper 2>&1 | Select-String 'privileged helper')"
Write-Host "log : $log"
Write-Host ""
Write-Host "COPY EVERYTHING ABOVE BACK TO THE ASSISTANT." -ForegroundColor Green
Write-Host "Press Enter to close this window..." -ForegroundColor Green
Stop-Transcript | Out-Null
Read-Host
