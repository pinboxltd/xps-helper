# Extract DA command class/selector pairs from the SMBIOS CallingInterface data.
#
# A CallingInterface record lists (class, selector, value) triples. Dell's DA
# transport addresses commands by exactly this pair, so knowing which pairs exist
# -- and which one is thermal -- is the missing link between the transport and the
# USTT payload. Read-only.
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

$smb = [Reflection.Assembly]::LoadFrom((Join-Path $dir 'SmbLib.dll'))
$parserT = $smb.GetType('Dell.CommandPowerManager.SmbiosTables.SmbiosTableParser')
$bytes = [byte[]](Get-CimInstance -Namespace root/wmi -ClassName MSSmBios_RawSMBiosTables).SMBiosData
$m = $parserT.GetMethod('ParseSmbiosTables')
$tables = $m.Invoke($null, @(, $bytes))
Write-Host "tables: $($tables.Count)"

Write-Host ""
Write-Host "=== CallingInterface triples (class, selector, value) ==="
foreach ($k in ($tables.Keys | Sort-Object)) {
    $t = $tables[$k]
    if ($t.GetType().Name -ne 'SmbiosTableCallingInterface') { continue }
    $raw = $t.RawTableData
    if (-not $raw) { continue }
    $body = $raw[2..($raw.Length - 1)]
    $triples = @()
    for ($i = 0; $i + 5 -lt $body.Length; $i += 6) {
        $cls = [BitConverter]::ToUInt16($body, $i)
        $sel = [BitConverter]::ToUInt16($body, $i + 2)
        $val = [BitConverter]::ToUInt16($body, $i + 4)
        if ($cls -eq 0xFFFF -and $sel -eq 0xFFFF) { break }
        $triples += ("{0:X4}/{1:X4}={2}" -f $cls, $sel, $val)
    }
    Write-Host ("  handle {0,-6} len={1,-4} n={2}" -f $k, $t.Length, $triples.Count)
    Write-Host ("      " + ($triples -join '  '))
}

Write-Host ""
Write-Host "=== distinct classes seen, and counts ==="
$counts = @{}
foreach ($k in $tables.Keys) {
    $t = $tables[$k]
    if ($t.GetType().Name -ne 'SmbiosTableCallingInterface') { continue }
    $raw = $t.RawTableData; if (-not $raw) { continue }
    $body = $raw[2..($raw.Length - 1)]
    for ($i = 0; $i + 5 -lt $body.Length; $i += 6) {
        $cls = [BitConverter]::ToUInt16($body, $i)
        if ($cls -eq 0xFFFF) { break }
        if ($counts.ContainsKey($cls)) { $counts[$cls]++ } else { $counts[$cls] = 1 }
    }
}
$counts.GetEnumerator() | Sort-Object Name | ForEach-Object { Write-Host ("   class 0x{0:X4} : {1} selectors" -f $_.Key, $_.Value) }
