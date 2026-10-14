# Snapshot every state Dell Power Manager's thermal control could plausibly touch.
#
# Run once, change the thermal mode in Dell Power Manager, run again, then diff
# the two files. Whatever moves is the mechanism.
param(
    [Parameter(Mandatory = $true)][string]$Tag,
    [string]$Dir = "$env:USERPROFILE\Desktop\xps-thermal-snapshots"
)
$ErrorActionPreference = 'SilentlyContinue'
New-Item -ItemType Directory -Force -Path $Dir | Out-Null
$lines = @()
function W { param([string]$T) Write-Host $T; $script:lines += $T }

W "XPS Helper thermal snapshot [$Tag]  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
W ("=" * 70)

# --- 1. BIOS attributes: names, values, and possible-value lists -----------
W ""
W "[1] BIOS attributes (root\dcim\sysman\biosattributes)"
$ns = 'root\dcim\sysman\biosattributes'
foreach ($class in @('EnumerationAttribute', 'IntegerAttribute', 'StringAttribute')) {
    foreach ($i in (Get-CimInstance -Namespace $ns -ClassName $class | Sort-Object AttributeName)) {
        $pv = ''
        if ($i.PSObject.Properties['PossibleValue'] -and $i.PossibleValue) { $pv = ($i.PossibleValue -join '|') }
        W ("  {0,-34} = {1,-24} [{2}]" -f $i.AttributeName, $i.CurrentValue, $pv)
    }
}

# --- 2. Power configuration, raw dump -------------------------------------
W ""
W "[2] powercfg /getactivescheme"
W ((powercfg /getactivescheme | Out-String).Trim())
W ""
W "[3] powercfg /list"
W ((powercfg /list | Out-String).Trim())
W ""
W "[4] full active-scheme PROCESSOR subgroup dump"
W ((powercfg /query SCHEME_CURRENT SUB_PROCESSOR | Out-String).Trim())
W ""
W "[5] full active-scheme dump (all subgroups)"
W ((powercfg /query SCHEME_CURRENT | Out-String).Trim())

# --- 3. Registry: everything Dell and everything power-related ------------
W ""
W "[6] registry exports"
$exports = @{
    'dell-hklm' = 'HKLM\SOFTWARE\Dell'
    'dell-policies' = 'HKLM\SOFTWARE\Policies\Dell'
    'dell-hkcu' = 'HKCU\Software\Dell'
    'power' = 'HKLM\SYSTEM\CurrentControlSet\Control\Power'
    'power-user' = 'HKLM\SYSTEM\CurrentControlSet\Control\Power\User\PowerSchemes'
}
foreach ($k in $exports.Keys) {
    $path = Join-Path $Dir "$Tag.$k.reg"
    $out = reg export $exports[$k] $path /y 2>&1
    W ("  {0,-14} -> {1}  ({2})" -f $k, (Split-Path $path -Leaf), ($out | Out-String).Trim())
}

# --- 4. Running Dell processes and their loaded modules -------------------
W ""
W "[7] Dell processes / services"
Get-Process | Where-Object { $_.Name -match 'Dell' } |
    ForEach-Object { W ("  proc {0} (pid {1})" -f $_.Name, $_.Id) }
Get-Service | Where-Object { $_.Name -match 'Dell' } |
    ForEach-Object { W ("  svc  {0} = {1}" -f $_.Name, $_.Status) }

$file = Join-Path $Dir "snapshot-$Tag.txt"
$lines | Set-Content -Path $file -Encoding UTF8
Write-Host "written $file"
