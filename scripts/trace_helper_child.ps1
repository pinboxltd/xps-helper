# Trace what the privileged task's child process actually does.
#
# The request IS being read (the child removes it) but the result files are never
# written, and the task exits 2. So the child starts and fails partway. This makes
# the child run a command that records its own environment, so we can see the
# paths it sees.
$ErrorActionPreference = 'Continue'
$d = "C:\Users\Stark-Laptop\Desktop\XPS-Helper"
$py = "$d\.venv\Scripts\python.exe"
$env:PYTHONPATH = "$d\src"

$reqPath = (& $py -c "import sys; sys.path.insert(0, r'$d\src'); from xpshelper import privileged as p; print(p.request_path())" 2>&1).Trim()
Write-Host "request path: $reqPath"

$trace = "$env:TEMP\xh-child-trace.txt"
Remove-Item $trace -ErrorAction SilentlyContinue
$out = "$env:TEMP\xh-t-out.txt"; $err = "$env:TEMP\xh-t-err.txt"; $rc = "$env:TEMP\xh-t-rc.txt"
Remove-Item $out, $err, $rc -ErrorAction SilentlyContinue

# The child command writes its own environment to a fixed path, plus the result.
$childCode = "import os;open(r'$trace','w').write('TEMP='+str(os.environ.get('TEMP'))+'\nTMP='+str(os.environ.get('TMP'))+'\ncwd='+os.getcwd()+'\nuser='+str(os.environ.get('USERNAME'))+'\nadmin='+str(os.environ.get('USERDOMAIN'))+'\n')"

$payload = [ordered]@{
    command   = @($py, "-c", $childCode)
    cwd       = $d
    out_file  = $out
    err_file  = $err
    code_file = $rc
}
$payload | ConvertTo-Json -Compress | Set-Content -Path $reqPath -Encoding UTF8
Write-Host "request written"

schtasks /run /tn "XPS Helper Privileged Helper" 2>&1 | Write-Host
$deadline = (Get-Date).AddSeconds(45)
while ((Get-Date) -lt $deadline -and -not (Test-Path $trace) -and -not (Test-Path $rc)) { Start-Sleep -Milliseconds 500 }

Write-Host ""
Write-Host "trace file : $(Test-Path $trace)"
if (Test-Path $trace) { Get-Content $trace | ForEach-Object { Write-Host "   $_" } }
Write-Host "out file   : $(Test-Path $out)"
if (Test-Path $out) { Get-Content $out | ForEach-Object { Write-Host "   out: $_" } }
Write-Host "err file   : $(Test-Path $err)"
if (Test-Path $err) { Get-Content $err | Select-Object -First 15 | ForEach-Object { Write-Host "   err: $_" } }
Write-Host "rc file    : $(Test-Path $rc)"
if (Test-Path $rc) { Write-Host "   rc = $(Get-Content $rc -Raw)" }
Write-Host "request gone: $(-not (Test-Path $reqPath))"

Write-Host ""
Write-Host "=== task detail ==="
schtasks /query /tn "XPS Helper Privileged Helper" /fo LIST /v 2>&1 |
    Select-String -Pattern "Last Result|Task To Run|Start In|Run As User" | ForEach-Object { Write-Host "   $_" }
