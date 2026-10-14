# Snapshot A: has Dell Power Manager changed anything in the firmware or power plan?
param([string]$OutFile = "$env:TEMP\dell-snapshot.txt")
$ErrorActionPreference = 'SilentlyContinue'
$lines = @()
function W { param([string]$T) Write-Host $T; $script:lines += $T }

$ns = 'root\dcim\sysman\biosattributes'
W "=== interface still reachable ==="
$iface = Get-CimInstance -Namespace $ns -ClassName 'BIOSAttributeInterface'
W ("  BIOSAttributeInterface: " + ($null -ne $iface))

W ""
W "=== FULL attribute list, looking for anything thermal ==="
$all = @()
foreach ($class in @('EnumerationAttribute', 'IntegerAttribute', 'StringAttribute')) {
    foreach ($i in (Get-CimInstance -Namespace $ns -ClassName $class)) {
        $pv = ''
        if ($i.PSObject.Properties['PossibleValue'] -and $i.PossibleValue) { $pv = ($i.PossibleValue -join '|') }
        $all += [pscustomobject]@{
            Name = "$($i.AttributeName)"; Value = "$($i.CurrentValue)"; Possible = $pv; Class = $class
        }
    }
}
W ("  total attributes: " + $all.Count)
$thermal = $all | Where-Object { $_.Name -match 'therm|thermal|fan|cool|quiet|perf|turbo' }
if ($thermal) { $thermal | ForEach-Object { W ("  {0,-28} = {1,-18} [{2}]" -f $_.Name, $_.Value, $_.Possible) } }
else { W "  (NO thermal/fan/performance attribute exists on this firmware)" }

W ""
W "=== active power scheme and the settings Dell would touch ==="
W ("  active: " + ((powercfg /getactivescheme) -join ''))
foreach ($pair in @(
    @('PROCTHROTTLEMAX', 'bc5038f7-23e0-4960-96da-33abaf5935ec'),
    @('PROCTHROTTLEMIN', '893dee8e-2bef-41e0-89c6-b55d0929964c'),
    @('PERFBOOSTMODE',   'be337238-0d82-4146-a960-4f3749d470c7'),
    @('SYSCOOLPOL',      '94d3a615-a899-4ac5-ae2b-e4d8f634367f')
)) {
    $q = powercfg /query SCHEME_CURRENT SUB_PROCESSOR $pair[1]
    $ac = ($q | Select-String 'Current AC Power Setting Index') -replace '.*:\s*', ''
    $dc = ($q | Select-String 'Current DC Power Setting Index') -replace '.*:\s*', ''
    W ("  {0,-16} AC={1,-12} DC={2}" -f $pair[0], ($ac -join '').Trim(), ($dc -join '').Trim())
}

W ""
W "=== did Dell create a power plan? ==="
W ((powercfg /list) -join "`n")

W ""
W "=== Dell CommandPowerManager registry (thermal config?) ==="
$found = $false
foreach ($root in @('HKLM:\SOFTWARE\Dell\CommandPowerManager', 'HKCU:\Software\Dell\CommandPowerManager',
                    'HKLM:\SOFTWARE\Policies\Dell\CommandPowerManager')) {
    Get-ChildItem $root -Recurse -ErrorAction SilentlyContinue | ForEach-Object {
        if ($_.Name -match 'thermal') { W ("  KEY: " + $_.Name); $script:found = $true }
        $props = Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue
        $props.PSObject.Properties | Where-Object { $_.Name -notmatch '^PS' } | ForEach-Object {
            if ($_.Name -match 'thermal' -or "$($_.Value)" -match 'thermal|Quiet|Cool|Optimized|UltraPerformance') {
                W ("  $($_.Name) = $($_.Value)"); $script:found = $true
            }
        }
    }
}
if (-not $found) { W "  (no thermal configuration written by Dell Power Manager)" }

$script:lines | Set-Content -Path $OutFile -Encoding UTF8
Write-Host "written $OutFile"
