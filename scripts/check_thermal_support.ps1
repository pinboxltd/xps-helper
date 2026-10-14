# Ask this machine's BIOS everything about thermal control. Read-only.
#
# Purpose: settle whether Dell's own software has a path this app lacks.
#   1. Is ThermalManagement present in ANY attribute class, including ones with
#      no PossibleValue (hidden/read-only attributes)?
#   2. What are its Modifiers? A "ProgHideLocal" or read-only modifier explains a
#      hidden attribute.
#   3. What else mentions thermal/power/performance in any class?
#   4. Which Dell host software is actually installed (drivers, services), since
#      that is what any Dell-side mechanism would talk through.
param([string]$OutFile = "$env:TEMP\xpshelper-thermal.txt")

$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'
$lines = @()
function W { param([string]$T) Write-Host $T; $script:lines += $T }

$ns = 'root\dcim\sysman\biosattributes'
W "XPS Helper: thermal-control reality check"
W ("=" * 70)
W ("elevated: " + ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))
W ""

W "--- 1. every attribute class, everything thermal/power/perf related ---"
foreach ($class in @('EnumerationAttribute', 'IntegerAttribute', 'StringAttribute', 'PassobjAttribute')) {
    $items = Get-CimInstance -Namespace $ns -ClassName $class
    W ("  {0}: {1} item(s)" -f $class, @($items).Count)
    foreach ($i in $items) {
        if ("$($i.AttributeName)" -match 'therm|fan|cool|quiet|perf|turbo|tdp|power|speed|mode') {
            W ("     {0,-30} = {1,-20} modifiers=[{2}]" -f $i.AttributeName, $i.CurrentValue, $i.Modifiers)
        }
    }
}
W ""

W "--- 2. is ThermalManagement or ThermalMode present at all? ---"
foreach ($name in @('ThermalManagement', 'ThermalMode', 'Thermal', 'PlatformProfile', 'PerformanceMode')) {
    $found = $false
    foreach ($class in @('EnumerationAttribute', 'IntegerAttribute', 'StringAttribute', 'PassobjAttribute')) {
        $hit = Get-CimInstance -Namespace $ns -ClassName $class -Filter "AttributeName='$name'"
        if ($hit) {
            $found = $true
            W ("  {0}: FOUND in {1} = '{2}' modifiers=[{3}]" -f $name, $class, $hit.CurrentValue, $hit.Modifiers)
        }
    }
    if (-not $found) { W ("  {0}: absent" -f $name) }
}
W ""

W "--- 3. Dell host software and services actually installed ---"
W "  WMI classes anywhere named Dell*:"
$dellClasses = @()
foreach ($scope in @('root\dcim\sysman', 'root\dcim\sysman\biosattributes', 'root\cimv2')) {
    $dellClasses += (Get-CimInstance -Namespace $scope -ClassName __CLASS |
        Where-Object { $_.Name -match 'Dell|SMM|SMBIOS' } |
        ForEach-Object { "$scope :: $($_.Name)" })
}
if ($dellClasses) { $dellClasses | ForEach-Object { W "     $_" } } else { W "     (none)" }
W ""
W "  Driver files present:"
foreach ($pat in @('DellDataVault', 'DDDriver', 'DellCmd', 'dell', 'DDVDataCollector')) {
    $hits = Get-ChildItem "$env:SystemRoot\System32\drivers" -Filter "*$pat*" -ErrorAction SilentlyContinue
    foreach ($h in $hits) { W ("     {0}" -f $h.Name) }
}
W ""
W "  Services/processes:"
Get-Service | Where-Object { $_.Name -match 'Dell|DDV|Trusted' } |
    ForEach-Object { W ("     service {0,-32} {1}" -f $_.Name, $_.Status) }
Get-Process | Where-Object { $_.Name -match 'Dell|DDV' } |
    ForEach-Object { W ("     process {0}" -f $_.Name) }
W ""

W "--- 4. is Dell Command | Configure or Power Manager installed? ---"
foreach ($p in @(
    "$env:ProgramFiles\Dell\Command Configure\X86_64\cctk.exe",
    "${env:ProgramFiles(x86)}\Dell\Command Configure\X86_64\cctk.exe",
    "$env:ProgramFiles\Dell\PowerManager\PowerManager.exe",
    "${env:ProgramFiles(x86)}\Dell\PowerManager\PowerManager.exe",
    "$env:ProgramFiles\Dell\DellOptimizer\DellOptimizer.exe"
)) { if (Test-Path $p) { W "  FOUND: $p" } }
W "  (nothing listed above means none of them are installed)"
W ""

W "--- 5. ACPI-WMI devices and their methods (Dell SMM path) ---"
W "  PNP0C14 instances:"
Get-PnpDevice -Class System -ErrorAction SilentlyContinue |
    Where-Object { $_.InstanceId -like '*PNP0C14*' } |
    ForEach-Object { W ("     {0}  [{1}]" -f $_.InstanceId, $_.Status) }

$script:lines | Set-Content -Path $OutFile -Encoding UTF8
Write-Host "written $OutFile"
