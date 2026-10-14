# Read the USTT frame through Dell's own code, now that the descriptor initialises.
#
# WmiDescriptor.Initialize() sets BufferLength = 0x8000 (32768). The earlier
# attempt at GetUsttInformation returned all-default values because that had not
# run. This initialises explicitly first, then reads.
#
# READ-ONLY: GetUsttInformation asks the firmware for its thermal tables.
$ErrorActionPreference = 'Continue'

$dir = "$env:ProgramFiles\Dell\CommandPowerManager"
[AppDomain]::CurrentDomain.add_AssemblyResolve({
    param($s, $e)
    $simple = ($e.Name -split ',')[0]
    if ($simple -notmatch '^(SmbLib|ObjLib|SystemInterop|ComponentService|Utilities|Alerts|Clarity)$') { return $null }
    $p = Join-Path $dir ($simple + '.dll')
    if (Test-Path $p) { return [Reflection.Assembly]::LoadFrom($p) }
    return $null
})

Write-Host "elevated: $(([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))"
Write-Host ""

# 1. Build an initialised descriptor and hand it to DataAbstractor if possible.
$smb = [Reflection.Assembly]::LoadFrom((Join-Path $dir 'SmbLib.dll'))
$wdT = $smb.GetType('Dell.CommandPowerManager.Wmi.WmiDescriptor')
$wd = [Activator]::CreateInstance($wdT)
$wdT.GetMethod('Initialize').Invoke($wd, @()) | Out-Null
Write-Host "descriptor ready: BufferLength=$($wdT.GetProperty('BufferLength').GetValue($wd)) version=$($wdT.GetProperty('Version').GetValue($wd))"
Write-Host ""

$si = [Reflection.Assembly]::LoadFrom((Join-Path $dir 'SystemInterop.dll'))
$t = $si.GetType('Dell.CommandPowerManager.SystemCalls.SystemInterop')

# SystemInterop has no public parameterless ctor; try each ctor shape.
$obj = $null
foreach ($c in $t.GetConstructors([Reflection.BindingFlags]::Public -bor [Reflection.BindingFlags]::NonPublic -bor [Reflection.BindingFlags]::Instance)) {
    $ps = $c.GetParameters()
    Write-Host "ctor($(($ps | ForEach-Object { $_.ParameterType.Name }) -join ', '))"
    if ($ps.Count -eq 0) {
        try { $obj = $c.Invoke(@()); Write-Host "  -> constructed" } catch { Write-Host "  -> $($_.Exception.InnerException.Message)" }
    }
}
if (-not $obj) { Write-Host "no usable ctor"; exit 1 }

Write-Host ""
foreach ($m in @('get_InterfaceBufferSize', 'get_WmiDescriptor', 'get_DataAbstractor', 'get_UseComponentService', 'IsAcpiWmiSupported')) {
    try {
        $mi = $t.GetMethod($m)
        if (-not $mi) { Write-Host ("{0,-24} : (no such member)" -f $m); continue }
        $v = $mi.Invoke($obj, @())
        if ($v -is [byte[]]) { Write-Host ("{0,-24} : byte[{1}]" -f $m, $v.Length) }
        else { Write-Host ("{0,-24} : {1}" -f $m, $v) }
    } catch {
        $inner = $_.Exception.InnerException
        Write-Host ("{0,-24} : EXC {1}" -f $m, $(if ($inner) { $inner.GetType().Name + ' ' + $inner.Message } else { $_.Exception.Message }))
    }
}

Write-Host ""
Write-Host "=== GetUsttInformation (firmware thermal tables) ==="
try {
    $tt = $t.GetMethod('GetUsttInformation').Invoke($obj, @())
    foreach ($p in $tt.GetType().GetProperties()) {
        try {
            $v = $p.GetValue($tt)
            if ($v -is [byte[]]) {
                Write-Host ("  {0} = byte[{1}]" -f $p.Name, $v.Length)
                for ($i = 0; $i -lt $v.Length; $i += 16) {
                    $chunk = $v[$i..([Math]::Min($i + 15, $v.Length - 1))]
                    Write-Host ("     {0:x4}  {1}" -f $i, (($chunk | ForEach-Object { $_.ToString('x2') }) -join ' '))
                }
            } elseif ($v -is [System.Collections.IEnumerable] -and $v -isnot [string]) {
                Write-Host ("  {0} = [{1}]" -f $p.Name, ((@($v) | Select-Object -First 16) -join ', '))
            } else { Write-Host ("  {0} = {1}" -f $p.Name, $v) }
        } catch { }
    }
} catch {
    $inner = $_.Exception.InnerException
    Write-Host "  EXC: $(if ($inner) { $inner.Message } else { $_.Exception.Message })"
}
