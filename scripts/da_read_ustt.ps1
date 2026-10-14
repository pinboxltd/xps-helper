# Send the USTT read command through Dell's REAL path and read the response.
#
# DataAbstractor.ExecuteDACommand(WmiDescriptor, Byte[]&) is what MakeBiosCall
# uses. The low-level BFn.DoBFn call only echoed because it bypasses this wrapper,
# which reads the response via GetOutputBuffer's DSCI framing.
#
# The read form (cbArg1 = 0) asks the firmware for its thermal tables and changes
# nothing. The buffer comes back by reference, so we decode it in place.
param(
    [int]$BufferSize = 32768,
    [switch]$Write,
    [uint32]$Mode = 0
)
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

$smb = [Reflection.Assembly]::LoadFrom((Join-Path $dir 'SmbLib.dll'))
$daT = $smb.GetType('Dell.CommandPowerManager.DataAbstraction.DataAbstractor')
$wdT = $smb.GetType('Dell.CommandPowerManager.Wmi.WmiDescriptor')
$BF = [Reflection.BindingFlags]::Public -bor [Reflection.BindingFlags]::NonPublic -bor [Reflection.BindingFlags]::Instance

$da = [Activator]::CreateInstance($daT)
$wd = ($daT.GetMethods($BF) | Where-Object { $_.Name -eq 'Initialize' } | Select-Object -First 1).Invoke($da, @())
$exec = $daT.GetMethods($BF) | Where-Object { $_.Name -eq 'ExecuteDACommand' } | Select-Object -First 1

Write-Host ("descriptor : {0} v{1} buffer={2}" -f `
    $wdT.GetProperty('Vendor').GetValue($wd),
    $wdT.GetProperty('Version').GetValue($wd),
    $wdT.GetProperty('BufferLength').GetValue($wd))

# Build the frame: 44-byte DA_CALL_STANDARD_INTERFACE at offset 0.
function New-Frame([uint16]$class, [uint16]$selector, [uint32]$arg1, [uint32]$arg2) {
    $buf = New-Object byte[] $BufferSize
    $buf[0] = [byte]($class -band 0xFF)
    $buf[1] = [byte](($class -shr 8) -band 0xFF)
    $buf[2] = [byte]($selector -band 0xFF)
    $buf[3] = [byte](($selector -shr 8) -band 0xFF)
    $a1 = [BitConverter]::GetBytes($arg1); [Array]::Copy($a1, 0, $buf, 4, 4)
    $a2 = [BitConverter]::GetBytes($arg2); [Array]::Copy($a2, 0, $buf, 8, 4)
    # cbRes1 = -3 as Dell pre-seeds it (status output; firmware overwrites on success)
    $r1 = [BitConverter]::GetBytes([uint32]4294967293); [Array]::Copy($r1, 0, $buf, 20, 4)
    return $buf
}

$arg1 = if ($Write) { [uint32]1 } else { [uint32]0 }
$buf = New-Frame 17 19 $arg1 $Mode
Write-Host ("sending    : Class=17 Selector=19 cbArg1={0} cbArg2={1}" -f $arg1, $Mode)

$ok = $null
try {
    # The second parameter is Byte[]& -- it must be supplied as the array itself,
    # and reflection writes the updated array back into this variable.
    $ok = $exec.Invoke($da, @([object]$wd, [object]$buf))
} catch {
    $inner = $_.Exception.InnerException
    Write-Host ("ExecuteDACommand EXC: " + $(if ($inner) { $inner.GetType().Name + ': ' + $inner.Message } else { $_.Exception.Message }))
    if ($inner -and $inner.InnerException) { Write-Host ("   inner: " + $inner.InnerException.Message) }
    exit 1
}
$result = $buf   # by-ref buffer, updated in place
Write-Host ("returned   : {0}   BufferLength field = {1}" -f $ok, [BitConverter]::ToUInt32($result, 20))

# Report where the firmware actually put data.
$nz = @()
for ($i = 0; $i -lt $result.Length; $i++) { if ($result[$i] -ne 0) { $nz += $i } }
Write-Host ("non-zero   : {0} bytes at {1}" -f $nz.Count, (($nz | Select-Object -First 40) -join ','))

if ($nz.Count -gt 0) {
    Write-Host ""
    Write-Host "=== response regions ==="
    $start = $nz[0]
    $end = [Math]::Min($result.Length, ($nz[-1] + 1))
    for ($i = 0; $i -lt $end; $i += 16) {
        $chunk = $result[$i..([Math]::Min($i + 15, $end - 1))]
        if (($chunk | Where-Object { $_ -ne 0 }).Count -eq 0) { continue }
        $hex = ($chunk | ForEach-Object { $_.ToString('x2') }) -join ' '
        $ascii = -join ($chunk | ForEach-Object { if ($_ -ge 32 -and $_ -lt 127) { [char]$_ } else { '.' } })
        Write-Host ("  {0:x4}  {1,-48}  {2}" -f $i, $hex, $ascii)
    }

    # Decode the DSCI framing GetOutputBuffer expects.
    Write-Host ""
    Write-Host "=== ASCII scan (looking for DSCI / USTT markers) ==="
    $text = -join ($result | ForEach-Object { if ($_ -ge 32 -and $_ -lt 127) { [char]$_ } else { '.' } })
    foreach ($m in [regex]::Matches($text, '[A-Z]{3,8}')) {
        if ($m.Value -notmatch '^\.+$') { Write-Host ("  at {0,6}: {1}" -f $m.Index, $m.Value) }
    }
}
