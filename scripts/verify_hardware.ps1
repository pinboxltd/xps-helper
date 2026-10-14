# Hardware verification for XPS Helper, run elevated.
#
# Everything here is READ-ONLY or a rewrite of a value to itself, so it cannot
# change how the machine behaves. It answers, from this machine's own firmware:
#
#   1. Does the Dell WMI-ACPI BIOS interface exist, and on which provider?
#   2. Which attributes does this BIOS actually expose, and what are their
#      permitted values? (This is what capability discovery is built on.)
#   3. Does the write path work, and does the firmware accept the exact tokens
#      the app sends?
#
# Usage (elevated):
#   powershell -ExecutionPolicy Bypass -File .\scripts\verify_hardware.ps1
#   powershell -ExecutionPolicy Bypass -File .\scripts\verify_hardware.ps1 -OutFile report.txt
#
# Why a separate script from the app: it reports the raw firmware values, so an
# assumption baked into the Python code can be corrected from real output rather
# than guesswork.
param(
    [string]$OutFile = "",
    [switch]$TestWrite
)

$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'

$script:Lines = @()
function W {
    param([string]$Text)
    Write-Host $Text
    $script:Lines += $Text
}

$ns = 'root\dcim\sysman\biosattributes'
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)

W "XPS Helper hardware verification"
W ("=" * 66)
W "elevated : $isAdmin"
W "user     : $(whoami)"
$cs = Get-CimInstance Win32_ComputerSystem
$bios = Get-CimInstance Win32_BIOS
W "model    : $($cs.Manufacturer) $($cs.Model)"
W "BIOS     : $($bios.SMBIOSBIOSVersion)"
W ""

if (-not $isAdmin) {
    W "WARNING: not elevated. The BIOS interface will report 'Access denied',"
    W "         which is a permissions result, not a missing feature."
    W ""
}

W "--- 1. interface presence ---"
try {
    $children = Get-CimInstance -Namespace 'root\dcim\sysman' -ClassName '__NAMESPACE' -ErrorAction Stop
    W ("namespace root\dcim\sysman children: " + (($children | ForEach-Object { $_.Name }) -join ', '))
} catch {
    W "could not enumerate root\dcim\sysman: $($_.Exception.Message)"
}

$iface = $null
foreach ($api in @('cim', 'wmi')) {
    try {
        if ($api -eq 'cim') {
            $iface = Get-CimInstance -Namespace $ns -ClassName 'BIOSAttributeInterface' -ErrorAction Stop
        } else {
            $iface = Get-WmiObject -Namespace $ns -Class 'BIOSAttributeInterface' -ErrorAction Stop
        }
        W "BIOSAttributeInterface reachable via $api"
        break
    } catch {
        W "BIOSAttributeInterface via ${api}: $($_.Exception.Message)"
    }
}
W ""

if ($null -eq $iface) {
    W "RESULT: the BIOS attribute interface is not usable from here."
    W "        If the message above says 'Access denied', re-run elevated."
    if ($OutFile) { $script:Lines | Set-Content -Path $OutFile -Encoding UTF8 }
    exit 1
}

W "--- 2. attributes this BIOS exposes ---"
$enum = Get-CimInstance -Namespace $ns -ClassName 'EnumerationAttribute'
$int = Get-CimInstance -Namespace $ns -ClassName 'IntegerAttribute'
$str = Get-CimInstance -Namespace $ns -ClassName 'StringAttribute'
W "EnumerationAttribute: $(@($enum).Count)   IntegerAttribute: $(@($int).Count)   StringAttribute: $(@($str).Count)"
W ""

function Show-Attr {
    param([string]$Name)
    $hit = $null
    foreach ($set in @($enum, $int, $str)) {
        $found = $set | Where-Object { $_.AttributeName -eq $Name }
        if ($found) { $hit = $found; break }
    }
    if ($hit) {
        $pv = ''
        if ($hit.PSObject.Properties['PossibleValue'] -and $hit.PossibleValue) {
            $pv = ($hit.PossibleValue -join ', ')
        }
        W ("  {0,-24} = {1,-14} possible: {2}" -f $hit.AttributeName, $hit.CurrentValue, $pv)
        return $hit
    }
    W ("  {0,-24} NOT PRESENT on this model" -f $Name)
    return $null
}

W "attributes XPS Helper uses:"
$thermal = Show-Attr 'ThermalManagement'
$charge = Show-Attr 'PrimaryBattChargeCfg'
$start = Show-Attr 'CustomChargeStart'
$stop = Show-Attr 'CustomChargeStop'
Show-Attr 'AdvBatteryChargeCfg'
W ""

W "--- 3. wmisecurity ---"
try {
    $pw = Get-CimInstance -Namespace 'root\dcim\sysman\wmisecurity' -ClassName 'PasswordObject' -ErrorAction Stop
    foreach ($p in $pw) { W ("  {0,-10} IsPasswordSet={1}" -f $p.NameId, $p.IsPasswordSet) }
} catch {
    W "  $($_.Exception.Message)"
}
W ""

if ($TestWrite) {
    W "--- 4. write path (rewriting each current value to itself) ---"
    W "    A rewrite-to-same-value cannot change machine behaviour."
    W ""
    $targets = @('PrimaryBattChargeCfg', 'CustomChargeStart', 'CustomChargeStop', 'ThermalManagement')
    foreach ($name in $targets) {
        $cur = $null
        foreach ($set in @($enum, $int, $str)) {
            $found = $set | Where-Object { $_.AttributeName -eq $name }
            if ($found) { $cur = $found.CurrentValue; break }
        }
        if ($null -eq $cur) { W ("  {0,-24} skipped (not present)" -f $name); continue }
        try {
            # SecHandle must be sent even with no password: omitting it makes the
            # firmware answer 'Invalid parameter' instead of a status code.
            $r = Invoke-CimMethod -InputObject $iface -MethodName 'SetAttribute' -Arguments @{
                AttributeName = $name
                AttributeValue = "$cur"
                SecType = 0
                SecHndCount = 0
                SecHandle = [byte[]]@()
            } -ErrorAction Stop
            $st = [int]$r.Status
            $txt = @('Success', 'Failed', 'Invalid Parameter', 'Access Denied',
                     'Not Supported', 'Memory Error', 'Protocol Error')[$st]
            W ("  {0,-24} = {1,-14} -> Status {2} ({3})" -f $name, $cur, $st, $txt)
        } catch {
            W ("  {0,-24} -> EXCEPTION: {1}" -f $name, $_.Exception.Message)
        }
    }
    W ""
}

W "--- summary ---"
if ($thermal) { W "thermal profiles : the firmware exposes ThermalManagement" }
else { W "thermal profiles : NOT available on this model (no ThermalManagement attribute)" }
if ($charge) {
    $vals = if ($charge.PSObject.Properties['PossibleValue'] -and $charge.PossibleValue) {
        ($charge.PossibleValue -join ', ')
    } else { '(none reported)' }
    W "charge modes     : $vals"
} else {
    W "charge modes     : NOT available on this model"
}
W ""
W "Report complete."

if ($OutFile) {
    $script:Lines | Set-Content -Path $OutFile -Encoding UTF8
    Write-Host "written to $OutFile"
}
