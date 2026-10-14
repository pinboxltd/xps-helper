# Diagnose why the privileged helper task does not report back.
#
# The task action is:  <venv python> -m xpshelper.elevated_run
# with no arguments. elevated_run then reads a well-known request file. This test
# writes a minimal request by hand, starts the task, and inspects every artefact.
$ErrorActionPreference = 'Continue'

$d = "C:\Users\Stark-Laptop\Desktop\XPS-Helper"
$py = "$d\.venv\Scripts\python.exe"
$env:PYTHONPATH = "$d\src"

Write-Host "=== 1. the well-known request path ==="
$reqPath = & $py -c "import sys; sys.path.insert(0, r'$d\src'); from xpshelper import privileged as p; print(p.request_path())" 2>&1
Write-Host "   $reqPath"

Write-Host ""
Write-Host "=== 2. write a minimal request by hand ==="
$out = "$env:TEMP\xh-th-out.txt"
$err = "$env:TEMP\xh-th-err.txt"
$rc  = "$env:TEMP\xh-th-rc.txt"
Remove-Item $out, $err, $rc -ErrorAction SilentlyContinue

$payload = [ordered]@{
    command  = @($py, "-c", "print('HELPER CHILD RAN')")
    cwd      = $d
    out_file = $out
    err_file = $err
    code_file = $rc
}
$payload | ConvertTo-Json -Compress | Set-Content -Path $reqPath -Encoding UTF8
Write-Host "   request written; exists = $(Test-Path $reqPath)"

Write-Host ""
Write-Host "=== 3. run the task ==="
schtasks /run /tn "XPS Helper Privileged Helper" 2>&1 | Write-Host

Write-Host "   waiting up to 45s for the rc sentinel..."
$deadline = (Get-Date).AddSeconds(45)
while ((Get-Date) -lt $deadline -and -not (Test-Path $rc)) { Start-Sleep -Milliseconds 500 }

Write-Host ""
Write-Host "=== 4. artefacts ==="
Write-Host "   request still present : $(Test-Path $reqPath)   (child removes it once read)"
Write-Host "   rc sentinel           : $(Test-Path $rc)"
if (Test-Path $rc)     { Write-Host "      rc = '$(Get-Content $rc -Raw)'" }
if (Test-Path $out)    { Write-Host "      out:"; Get-Content $out | ForEach-Object { Write-Host "        $_" } }
if (Test-Path $err)    { Write-Host "      err:"; Get-Content $err | Select-Object -First 20 | ForEach-Object { Write-Host "        $_" } }

Write-Host ""
Write-Host "=== 5. task last result ==="
schtasks /query /tn "XPS Helper Privileged Helper" /fo LIST /v 2>&1 |
    Select-String -Pattern "Last Run Time|Last Result|Status|Logon Mode|Run As User" | ForEach-Object { Write-Host "   $_" }

Write-Host ""
Write-Host "=== 6. does the task action run at all, standalone? ==="
Write-Host "   direct (unelevated) invocation of the same command:"
& $py -m xpshelper.elevated_run 2>&1 | Select-Object -First 5 | ForEach-Object { Write-Host "     $_" }
Write-Host "   (it should complain about a missing request, which proves it starts)"
