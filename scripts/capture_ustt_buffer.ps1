# Capture the real USTT interface buffer.
#
# GetUsttInformation is READ-ONLY (it asks the firmware for the thermal tables);
# only SetUsttInformation writes. So this exercises Dell's own code path and
# observes the buffer layout without changing anything.
#
# This is the safe way to learn the frame format: reverse-engineering gave us the
# constants (256-byte buffer, 32-bit command 0x0868B2) but not the byte layout.
$ErrorActionPreference = 'Continue'
$dir = "$env:ProgramFiles\Dell\CommandPowerManager"
$resolver = {
    param($s, $e)
    $simple = ($e.Name -split ',')[0]
    if ($simple -notmatch '^(SmbLib|ObjLib|SystemInterop|ComponentService|Utilities|Alerts|Clarity)$') { return $null }
    $p = Join-Path $dir ($simple + '.dll')
    if (Test-Path $p) { return [Reflection.Assembly]::LoadFrom($p) }
    return $null
}
[AppDomain]::CurrentDomain.add_AssemblyResolve($resolver)

$si = [Reflection.Assembly]::LoadFrom((Join-Path $dir 'SystemInterop.dll'))
$t = $si.GetType('Dell.CommandPowerManager.SystemCalls.SystemInterop')
$obj = $t.GetConstructors([Reflection.BindingFlags]::Public -bor [Reflection.BindingFlags]::NonPublic -bor [Reflection.BindingFlags]::Instance)[0].Invoke(@())

function Show-Hex {
    param([byte[]]$Bytes, [string]$Label, [int]$Limit = 256)
    if (-not $Bytes) { Write-Host "  $Label : (null)"; return }
    Write-Host "  $Label : $($Bytes.Length) bytes"
    $n = [Math]::Min($Limit, $Bytes.Length)
    for ($i = 0; $i -lt $n; $i += 16) {
        $chunk = $Bytes[$i..([Math]::Min($i + 15, $n - 1))]
        $hex = ($chunk | ForEach-Object { $_.ToString('x2') }) -join ' '
        $ascii = -join ($chunk | ForEach-Object { if ($_ -ge 32 -and $_ -lt 127) { [char]$_ } else { '.' } })
        Write-Host ("    {0:x4}  {1,-47}  {2}" -f $i, $hex, $ascii)
    }
}

Write-Host "=== InterfaceBufferSize (the frame length Dell uses) ==="
try {
    $size = $t.GetMethod('get_InterfaceBufferSize').Invoke($obj, @())
    Write-Host "  InterfaceBufferSize = $size"
} catch {
    $inner = $_.Exception.InnerException
    Write-Host "  EXC: $(if ($inner) { $inner.Message } else { $_.Exception.Message })"
}

Write-Host ""
Write-Host "=== UseComponentService / AcpiWmiSupported ==="
foreach ($m in @('get_UseComponentService', 'IsAcpiWmiSupported')) {
    try {
        $mi = $t.GetMethod($m)
        Write-Host ("  {0} = {1}" -f $m, $mi.Invoke($obj, @()))
    } catch {
        $inner = $_.Exception.InnerException
        Write-Host "  $m -> EXC $(if ($inner) { $inner.Message } else { $_.Exception.Message })"
    }
}

Write-Host ""
Write-Host "=== GetUsttInformation (READ-ONLY firmware query) ==="
try {
    $tt = $t.GetMethod('GetUsttInformation').Invoke($obj, @())
    if ($tt) {
        foreach ($p in $tt.GetType().GetProperties()) {
            try {
                $v = $p.GetValue($tt)
                if ($v -is [byte[]]) { Show-Hex $v "prop $($p.Name)" }
                else {
                    if ($v -is [System.Collections.IEnumerable] -and $v -isnot [string]) {
                        $items = @($v | ForEach-Object { $_ })
                        Write-Host ("  {0} = [{1}]" -f $p.Name, (($items | Select-Object -First 16) -join ', '))
                    } else {
                        Write-Host ("  {0} = {1}" -f $p.Name, $v)
                    }
                }
            } catch { }
        }
    } else { Write-Host "  returned null" }
} catch {
    $inner = $_.Exception.InnerException
    Write-Host "  EXC: $(if ($inner) { $inner.Message } else { $_.Exception.Message })"
}
