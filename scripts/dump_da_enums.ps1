# Resolve Dell's DA class/selector mapping for thermal.
#
# SmbLib defines AcpiWmi.CallingInterfaceClassValueEnum (the classes the BIOS
# advertises) and AcpiWmi.* enums for command selectors. If the thermal class is
# named there, it identifies which CallingInterface entry the USTT command uses --
# the last missing link between the transport and the payload.
#
# Read-only: enumerates enum values from the assembly.
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

Write-Host "=== AcpiWmi namespace enums ==="
$enums = $smb.GetTypes() | Where-Object { $_.IsEnum -and $_.FullName -like '*AcpiWmi*' }
foreach ($e in $enums) {
    Write-Host "--- $($e.FullName)  (underlying: $([Enum]::GetUnderlyingType($e).Name))"
    foreach ($n in [Enum]::GetNames($e)) {
        # Some of these enums are unsigned 32-bit; casting to [int] throws, so go
        # through the underlying value and format as hex as well.
        $raw = [Enum]::Parse($e, $n)
        $u = [Convert]::ToUInt32($raw)
        Write-Host ("      {0,-40} = {1,10}  0x{2:X4}" -f $n, $u, $u)
    }
}

Write-Host ""
Write-Host "=== enums anywhere whose name mentions thermal/command/subcommand ==="
$smb.GetTypes() | Where-Object { $_.IsEnum -and $_.Name -match 'Therm|Command|SubCommand|Class|Select' } |
    ForEach-Object {
        Write-Host "--- $($_.FullName)"
        foreach ($n in [Enum]::GetNames($_)) {
            $raw = [Enum]::Parse($_, $n)
            $u = [Convert]::ToUInt32($raw)
            Write-Host ("      {0,-40} = {1,10}  0x{2:X4}" -f $n, $u, $u)
        }
    }
