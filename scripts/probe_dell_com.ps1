# Can we drive Dell's COM+ service directly (already running elevated)?
#
# If yes, thermal control needs NO UAC: the privileged service does the work,
# exactly as Dell's own UI does. Run UNELEVATED to prove it.
$ErrorActionPreference = 'Continue'

Write-Host "=== am I elevated? ==="
$elev = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
Write-Host "  elevated = $elev  (we want False: proves no UAC needed)"

Write-Host ""
Write-Host "=== Dell Command Power Manager COM registrations ==="
foreach ($clsid in @('{28F2DC5E-0F47-3F21-8DF6-57CCB1D9CB28}',
                     '{8FCAFA01-6CDC-3EDE-9390-AF9FB9A945C3}')) {
    $k = "HKLM:\SOFTWARE\Classes\CLSID\$clsid"
    if (Test-Path $k) {
        $p = Get-ItemProperty $k
        Write-Host "  $clsid -> $($p.'(default)')"
        foreach ($sub in @('InprocServer32', 'LocalServer32', 'ProgID', 'TypeLib')) {
            if (Test-Path "$k\$sub") {
                $v = Get-ItemProperty "$k\$sub"
                Write-Host "      $sub = $($v.'(default)')"
            }
        }
    }
}

Write-Host ""
Write-Host "=== try to instantiate the service object unelevated ==="
foreach ($id in @('Dell.CommandPowerManager.Service.ComponentService',
                  '{28F2DC5E-0F47-3F21-8DF6-57CCB1D9CB28}')) {
    Write-Host "  -- $id"
    try {
        $obj = New-Object -ComObject $id -ErrorAction Stop
        Write-Host "     CREATED: $($obj.GetType().FullName)"
        Write-Host "     members:"
        $obj | Get-Member -MemberType Method, Property |
            Select-Object -First 40 |
            ForEach-Object { Write-Host ("        {0} {1}" -f $_.MemberType, $_.Name) }
    } catch {
        Write-Host "     FAILED: $($_.Exception.Message)"
    }
}

Write-Host ""
Write-Host "=== search the whole CLSID tree for anything thermal/power related ==="
Get-ChildItem 'HKLM:\SOFTWARE\Classes\CLSID' -ErrorAction SilentlyContinue | ForEach-Object {
    $d = (Get-ItemProperty $_.PSPath -Name '(default)' -ErrorAction SilentlyContinue).'(default)'
    if ($d -match 'PowerManager|Thermal|Instrumentation|Asimov|TechHub') {
        Write-Host "  $($_.PSChildName) = $d"
        foreach ($sub in @('InprocServer32', 'LocalServer32')) {
            if (Test-Path "$($_.PSPath)\$sub") {
                Write-Host "      $sub = $((Get-ItemProperty "$($_.PSPath)\$sub").'(default)')"
            }
        }
    }
}
