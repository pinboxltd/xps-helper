# Diagnose why the privileged helper install produces no output.
#
# The install path is: this app (unelevated)
#   -> elevate._shell_execute(cmd /c "powershell -File install.ps1" > out 2> err 2>&1& echo %ERRORLEVEL% > rc)
#   -> cmd waits for the rc sentinel, then the parent echoes out/err
# "installed: False" with no output means the sentinel appeared but the files were
# empty, or the script ran and silently failed. This isolates which.
$ErrorActionPreference = 'Continue'

$d = "C:\Users\Stark-Laptop\Desktop\XPS-Helper"
$py = "$d\.venv\Scripts\python.exe"
$env:PYTHONPATH = "$d\src"

Write-Host "=== 1. reproduce the elevated spawn with a trivial script ==="
$probe = "$env:TEMP\xh-probe.ps1"
@'
$ErrorActionPreference = 'Stop'
try {
    "probe ran at $(Get-Date -Format o)" | Out-File -FilePath "$env:TEMP\xh-probe-result.txt" -Encoding utf8
    "is elevated: " + ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) | Out-File -Append -FilePath "$env:TEMP\xh-probe-result.txt" -Encoding utf8
    exit 0
} catch {
    "FAILED: $($_.Exception.Message)" | Out-File -FilePath "$env:TEMP\xh-probe-result.txt" -Encoding utf8
    exit 1
}
'@ | Set-Content -Path $probe -Encoding utf8

Remove-Item "$env:TEMP\xh-probe-result.txt" -ErrorAction SilentlyContinue

# Call the app's own elevation helper, exactly as --install does.
& $py -c @"
import sys
sys.path.insert(0, r'$d\src')
from xpshelper import privileged as p
out = p.run_elevated_once(open(r'$probe', encoding='utf-8').read())
print('run_elevated_once returned:', repr(out))
"@ 2>&1

Write-Host ""
Write-Host "=== 2. did the elevated script actually run? ==="
if (Test-Path "$env:TEMP\xh-probe-result.txt") {
    Write-Host "YES - elevated execution works:"
    Get-Content "$env:TEMP\xh-probe-result.txt" | ForEach-Object { Write-Host "   $_" }
} else {
    Write-Host "NO - the elevated script never wrote its result."
}

Write-Host ""
Write-Host "=== 3. what does the install script do when run elevated? ==="
& $py -c @"
import sys
sys.path.insert(0, r'$d\src')
from xpshelper import privileged as p
print(p.install_script())
"@ 2>&1 | Select-Object -First 6
