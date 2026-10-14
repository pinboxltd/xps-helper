# Capture Dell's WMI descriptor and the USTT frame, ELEVATED, via the helper.
#
# WmiDescriptor::Initialize (SmbLib) queries root\wmi :: WMI_Query for
# InstanceName 'ACPI\PNP0C14\0_0', reads QDATA + Bytes, requires a "DELL" vendor
# and " WMI" suffix in the signature, then sets Version and BufferLength. Those
# last two are exactly what GetUsttInformation needs (and what returned null
# unelevated), so this is the missing setup.
#
# READ-ONLY: queries WMI and reads properties. Nothing is written.
$ErrorActionPreference = 'Continue'

Write-Host "=== elevated: $(([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))"
Write-Host ""

Write-Host "=== 1. WMI_Query instances (Dell's descriptor source) ==="
try {
    $q = Get-CimInstance -Namespace root/wmi -ClassName WMI_Query -ErrorAction Stop
    Write-Host "  count: $(@($q).Count)"
    foreach ($i in @($q)) {
        Write-Host "  InstanceName: $($i.InstanceName)  Active=$($i.Active)"
        $qd = $i.QDATA
        if ($qd) {
            Write-Host "    QDATA type: $($qd.GetType().FullName)"
            foreach ($p in ($qd | Get-Member -MemberType Property | ForEach-Object { $_.Name })) {
                if ($p -like 'PS*' -or $p -eq 'CimClass' -or $p -eq 'CimInstanceProperties' -or $p -eq 'CimSystemProperties') { continue }
                $v = $qd.$p
                if ($v -is [byte[]]) {
                    Write-Host ("    QDATA.{0} = byte[{1}] {2}" -f $p, $v.Length, (($v | Select-Object -First 40 | ForEach-Object { $_.ToString('x2') }) -join ' '))
                } else {
                    Write-Host ("    QDATA.{0} = {1}" -f $p, $v)
                }
            }
        } else { Write-Host "    (QDATA null)" }
        $b = $i.Bytes
        if ($b) { Write-Host ("    Bytes = byte[{0}] {1}" -f $b.Length, (($b | Select-Object -First 32 | ForEach-Object { $_.ToString('x2') }) -join ' ')) }
    }
    if (@($q).Count -eq 0) { Write-Host "  (no instances returned)" }
} catch {
    Write-Host "  FAILED: $($_.Exception.Message)"
}

Write-Host ""
Write-Host "=== 2. Dell's own parser on the descriptor ==="
$dir = "$env:ProgramFiles\Dell\CommandPowerManager"
[AppDomain]::CurrentDomain.add_AssemblyResolve({
    param($s, $e)
    $simple = ($e.Name -split ',')[0]
    if ($simple -notmatch '^(SmbLib|ObjLib|SystemInterop|ComponentService|Utilities|Alerts|Clarity)$') { return $null }
    $p = Join-Path $dir ($simple + '.dll')
    if (Test-Path $p) { return [Reflection.Assembly]::LoadFrom($p) }
    return $null
})
$smb = [Reflection.Assembly]::LoadFrom((Join-Path $dir 'SmbLib.dll'))
$wdT = $smb.GetType('Dell.CommandPowerManager.Wmi.WmiDescriptor')
Write-Host "  WmiDescriptor is: $(if ($wdT.IsValueType) { 'struct' } else { 'class' })"
$wd = [Activator]::CreateInstance($wdT)
try {
    $wdT.GetMethod('Initialize').Invoke($wd, @()) | Out-Null
    Write-Host "  Initialize() OK"
} catch {
    $inner = $_.Exception.InnerException
    Write-Host "  Initialize() EXC: $(if ($inner) { $inner.Message } else { $_.Exception.Message })"
}
foreach ($p in @('Vendor', 'Signature', 'Version', 'BufferLength', 'IsInitialized')) {
    try { Write-Host ("  {0,-14} = {1}" -f $p, $wdT.GetProperty($p).GetValue($wd)) } catch { }
}

Write-Host ""
Write-Host "=== 3. InterfaceBufferSize (needs the descriptor above) ==="
try {
    $si = [Reflection.Assembly]::LoadFrom((Join-Path $dir 'SystemInterop.dll'))
    $t = $si.GetType('Dell.CommandPowerManager.SystemCalls.SystemInterop')
    $obj = $t.GetConstructors([Reflection.BindingFlags]::Public -bor [Reflection.BindingFlags]::NonPublic -bor [Reflection.BindingFlags]::Instance)[0].Invoke(@())
    Write-Host "  InterfaceBufferSize = $($t.GetMethod('get_InterfaceBufferSize').Invoke($obj, @()))"
    Write-Host "  IsAcpiWmiSupported  = $($t.GetMethod('IsAcpiWmiSupported').Invoke($obj, @()))"
    $tt = $t.GetMethod('GetUsttInformation').Invoke($obj, @())
    Write-Host "  GetUsttInformation:"
    foreach ($p in $tt.GetType().GetProperties()) {
        try {
            $v = $p.GetValue($tt)
            if ($v -is [byte[]]) { Write-Host ("    {0} = byte[{1}] {2}" -f $p.Name, $v.Length, (($v | Select-Object -First 48 | ForEach-Object { $_.ToString('x2') }) -join ' ')) }
            elseif ($v -is [System.Collections.IEnumerable] -and $v -isnot [string]) { Write-Host ("    {0} = [{1}]" -f $p.Name, ((@($v) | Select-Object -First 12) -join ', ')) }
            else { Write-Host ("    {0} = {1}" -f $p.Name, $v) }
        } catch { }
    }
} catch {
    $inner = $_.Exception.InnerException
    Write-Host "  EXC: $(if ($inner) { $inner.Message } else { $_.Exception.Message })"
}
