<#
.SYNOPSIS
    Hardware smoke test for XPS Helper on a Dell XPS (13 7390 2-in-1).

.DESCRIPTION
    Run this ON THE LAPTOP, once, before trusting the app. It installs nothing,
    downloads nothing, and changes nothing unless you pass -TestWrite.

    XPS Helper drives the BIOS directly over WMI-ACPI -- the firmware's own
    "agentless" management interface -- so no Dell software is involved. This
    script verifies that interface exists on your machine and prints its raw
    contents.

    Why this exists: XPS Helper was developed on a machine that is not an XPS,
    so its assumptions about your firmware were checked against public sources
    and canned test data, not against your laptop. This closes that gap in about
    ten seconds. If anything is wrong, the raw output here is exactly what is
    needed to fix the parser.

.PARAMETER TestWrite
    Also verify write access by writing each attribute's *current* value back to
    itself. This exercises the full write path (privileges, BIOS acceptance)
    without changing any setting.

.PARAMETER SkipCctk
    Skip the optional Dell Command | Configure section.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\smoke_test_windows.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\smoke_test_windows.ps1 -TestWrite
#>
[CmdletBinding()]
param(
    [switch]$TestWrite,
    [switch]$SkipCctk
)

$ErrorActionPreference = 'Continue'
$script:Passed = @()
$script:Problems = @()

# The attributes XPS Helper cares about.
$TargetAttributes = @('ThermalManagement', 'PrimaryBattChargeCfg', 'AdvBatteryChargeCfg')
$AttributeClasses = @('EnumerationAttribute', 'IntegerAttribute', 'StringAttribute', 'PassobjAttribute')
$BiosNs = 'root\dcim\sysman\biosattributes'

function Section([string]$Title) {
    Write-Host ''
    Write-Host ('=' * 72)
    Write-Host $Title
    Write-Host ('=' * 72)
}
function Note([string]$Text) { Write-Host "  $Text" }
function Good([string]$Text) { Write-Host "  [ ok ] $Text"; $script:Passed += $Text }
function Bad([string]$Text)  { Write-Host "  [fail] $Text" -ForegroundColor Red; $script:Problems += $Text }
function Warn([string]$Text) { Write-Host "  [warn] $Text" -ForegroundColor Yellow }

function Test-Administrator {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

Write-Host 'XPS Helper - Windows hardware smoke test (native WMI-ACPI path)'
Write-Host "Date: $(Get-Date -Format 'u')   PowerShell: $($PSVersionTable.PSVersion)"

# ---------------------------------------------------------------------------
Section '1. System identity'
# ---------------------------------------------------------------------------
try {
    $cs = Get-CimInstance -ClassName Win32_ComputerSystem
    $bios = Get-CimInstance -ClassName Win32_BIOS
    Note "Manufacturer : $($cs.Manufacturer)"
    Note "Model        : $($cs.Model)"
    Note "BIOS version : $($bios.SMBIOSBIOSVersion)"
    if ($cs.Manufacturer -match 'Dell') { Good 'Manufacturer is Dell.' }
    else { Bad "Not a Dell: '$($cs.Manufacturer)'." }
    if ($cs.Model -match '7390') { Good 'Model is in the XPS 13 7390 family.' }
    else { Warn "Model '$($cs.Model)' is not the 7390 this was designed for; it may still work." }
} catch { Bad "Could not read system identity: $_" }

# ---------------------------------------------------------------------------
Section '2. Privileges'
# ---------------------------------------------------------------------------
if (Test-Administrator) { Good 'Running elevated: settings can be changed.' }
else { Warn 'NOT elevated. Reading works; changing settings will be denied. Re-run as administrator.' }

# ---------------------------------------------------------------------------
Section '3. Native BIOS interface (WMI-ACPI) - the path XPS Helper uses'
# ---------------------------------------------------------------------------
$interfaceOk = $false
try {
    $null = Get-CimInstance -Namespace $BiosNs -ClassName 'BIOSAttributeInterface' -ErrorAction Stop
    $interfaceOk = $true
    Good "Found BIOSAttributeInterface in $BiosNs (no Dell software needed)."
} catch {
    Bad "BIOSAttributeInterface not found in $BiosNs."
    Note "This means the BIOS does not expose the agentless management interface."
    Note "Error: $($_.Exception.Message)"
    Note ''
    Note 'Diagnostics: which WMI namespaces/classes DO exist?'
    try {
        Get-CimInstance -Namespace 'root/dcim' -ClassName '__NAMESPACE' -ErrorAction Stop |
            ForEach-Object { Note "  namespace: root\dcim\$($_.Name)" }
    } catch { Note '  (no root\dcim namespace)' }
    try {
        Get-CimInstance -Namespace 'root/wmi' -ClassName '__CLASS' -ErrorAction Stop |
            Where-Object { $_.Name -match 'Dell|Bios|BIOS|Attribute|Enum' } |
            ForEach-Object { Note "  root\wmi class: $($_.Name)" }
    } catch { }
}

if ($interfaceOk) {
    # BIOS administrator password state: writes are denied without it.
    try {
        $pw = Get-CimInstance -Namespace 'root/dcim/sysman/wmisecurity' `
                -ClassName 'PasswordObject' -Filter "NameId='Admin'" -ErrorAction Stop
        if ($pw) {
            if ($pw.IsPasswordSet) {
                Warn 'A BIOS administrator password is set; XPS Helper will need it to write.'
            } else {
                Good 'No BIOS administrator password is set.'
            }
        }
    } catch { Warn 'Could not determine whether a BIOS administrator password is set.' }

    foreach ($attribute in $TargetAttributes) {
        Write-Host ''
        Write-Host "  --- $attribute ---"
        $found = $false
        foreach ($class in $AttributeClasses) {
            try {
                $items = Get-CimInstance -Namespace $BiosNs -ClassName $class -ErrorAction Stop |
                    Where-Object { $_.AttributeName -eq $attribute }
            } catch { continue }
            if (-not $items) { continue }

            $found = $true
            foreach ($item in $items) {
                Note "class          : $class"
                Note "AttributeName  : $($item.AttributeName)"
                Note "CurrentValue   : $($item.CurrentValue)"
                $pv = $item.PossibleValues
                if ($null -ne $pv) {
                    if ($pv -is [array]) {
                        Note "PossibleValues : $($pv -join ' | ')"
                    } else {
                        Note "PossibleValues : $pv"
                    }
                } else {
                    Warn "$attribute does not report PossibleValues - XPS Helper will fall back to showing all documented values."
                }
                Note 'All properties returned by the BIOS for this attribute:'
                ($item | Format-List * | Out-String) -split "`r?`n" |
                    Where-Object { $_.Trim() } | ForEach-Object { Write-Host "      $_" }
            }
        }
        if (-not $found) {
            Warn "$attribute is not present in this BIOS."
        } else {
            Good "$attribute is exposed by the BIOS."
        }
    }

    if ($TestWrite) {
        Write-Host ''
        Write-Host '  --- write test (writes each current value back to itself) ---'
        foreach ($attribute in $TargetAttributes) {
            $entry = $null
            foreach ($class in $AttributeClasses) {
                try {
                    $entry = Get-CimInstance -Namespace $BiosNs -ClassName $class -ErrorAction Stop |
                        Where-Object { $_.AttributeName -eq $attribute } | Select-Object -First 1
                } catch { continue }
                if ($entry) { break }
            }
            if (-not $entry) { continue }

            $value = $entry.CurrentValue
            Note "Writing $attribute=$value (unchanged)"
            try {
                $iface = Get-CimInstance -Namespace $BiosNs -ClassName 'BIOSAttributeInterface' -ErrorAction Stop
                $result = Invoke-CimMethod -InputObject $iface -MethodName 'SetAttribute' `
                            -Arguments @{ AttributeName = $attribute; AttributeValue = "$value" } `
                            -ErrorAction Stop
                switch ([int]$result.Status) {
                    0 { Good "$attribute write succeeded (status 0). Nothing was changed." }
                    3 { Warn "$attribute write needs the BIOS administrator password (status 3)." }
                    4 { Warn "$attribute is not writable on this model (status 4)." }
                    default { Bad "$attribute write returned status $($result.Status)." }
                }
            } catch {
                Bad "$attribute write threw: $($_.Exception.Message)"
            }
        }
    } else {
        Write-Host ''
        Note 'Write test skipped. Re-run with -TestWrite to verify write access (it only rewrites current values).'
    }
}

# ---------------------------------------------------------------------------
Section '4. Dell Command | Configure (OPTIONAL - not required)'
# ---------------------------------------------------------------------------
if ($SkipCctk) {
    Warn 'Skipped (-SkipCctk).'
} else {
    $cctk = @(
        "${env:ProgramFiles(x86)}\Dell\Command Configure\X86_64\cctk.exe",
        "$env:ProgramFiles\Dell\Command Configure\X86_64\cctk.exe"
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1

    if ($cctk) {
        Good "cctk found at $cctk (optional fallback; XPS Helper does not need it)."
    } else {
        Note 'cctk is not installed. This is fine - XPS Helper does not require it.'
        Note 'Do not install it just for this app.'
    }
}

# ---------------------------------------------------------------------------
Section '5. Battery'
# ---------------------------------------------------------------------------
try {
    $battery = Get-CimInstance -Namespace 'root/cimv2' -ClassName Win32_Battery -ErrorAction Stop
    if ($battery) {
        foreach ($b in $battery) {
            Note "Name                : $($b.Name)"
            Note "Charge              : $($b.EstimatedChargeRemaining)%"
            Note "BatteryStatus       : $($b.BatteryStatus)  (1=discharging, 2=on AC, 6=charging)"
        }
        $static = Get-CimInstance -Namespace 'root/wmi' -ClassName BatteryStaticData -ErrorAction SilentlyContinue
        $full = Get-CimInstance -Namespace 'root/wmi' -ClassName BatteryFullChargedCapacity -ErrorAction SilentlyContinue
        if ($static -and $full -and $static.DesignedCapacity -gt 0) {
            $health = [math]::Round($full.FullChargedCapacity / $static.DesignedCapacity * 100, 1)
            Note "Health              : $health%  ($($full.FullChargedCapacity) / $($static.DesignedCapacity) mWh)"
        }
        Good 'Battery readable.'
    } else { Warn 'Win32_Battery returned nothing.' }
} catch { Bad "Battery query failed: $_" }

# ---------------------------------------------------------------------------
Section '6. Temperature sources'
# ---------------------------------------------------------------------------
$foundTemp = $false
try {
    $lhm = Get-CimInstance -Namespace 'root/LibreHardwareMonitor' -ClassName Sensor -ErrorAction Stop |
        Where-Object { $_.SensorType -eq 'Temperature' }
    if ($lhm) {
        $lhm | Select-Object -First 8 | ForEach-Object { Note "$($_.Name) = $($_.Value) C" }
        Good 'LibreHardwareMonitor sensors found (open source, not Dell software).'
        $foundTemp = $true
    }
} catch { Note 'LibreHardwareMonitor not running (optional).' }

if (-not $foundTemp) {
    try {
        $acpi = Get-CimInstance -Namespace 'root/wmi' -ClassName MSAcpi_ThermalZoneTemperature -ErrorAction Stop
        foreach ($zone in $acpi) {
            Note "$($zone.InstanceName) = $([math]::Round($zone.CurrentTemperature / 10 - 273.15, 1)) C"
        }
        if ($acpi) { Good 'ACPI thermal zone available as a fallback.'; $foundTemp = $true }
    } catch { Note 'ACPI thermal zone not available.' }
}
if (-not $foundTemp) { Warn 'No temperature source found.' }

# ---------------------------------------------------------------------------
Section '7. Fan speed (read-only)'
# ---------------------------------------------------------------------------
$fanFound = $false
try {
    $fans = Get-CimInstance -Namespace 'root/dcim/sysman' -ClassName 'DCIM_NumericSensor' -ErrorAction Stop |
        Where-Object { $_.ElementName -like 'Fan*' -or $_.BaseUnits -match 'RPM|Revolutions' }
    if ($fans) {
        $fans | ForEach-Object { Note "$($_.ElementName) = $($_.CurrentReading) RPM" }
        Good 'Fan speed readable.'
        $fanFound = $true
    }
} catch { }
if (-not $fanFound) {
    Warn 'Fan speed not readable. Note that MANUAL fan control is not possible on an XPS 13:'
    Note 'the fan is owned by the embedded controller / BIOS.'
}

# ---------------------------------------------------------------------------
Section 'SUMMARY'
# ---------------------------------------------------------------------------
Write-Host "Passed : $($script:Passed.Count)"
Write-Host "Problems: $($script:Problems.Count)"
if ($script:Problems.Count -gt 0) {
    Write-Host ''
    Write-Host 'Problems found:' -ForegroundColor Red
    $script:Problems | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
}
Write-Host ''
Write-Host 'Next: run  xps-helper doctor  and keep both outputs.'
if (-not $interfaceOk) {
    Write-Host ''
    Write-Host 'The native interface was not found. XPS Helper will still monitor the' -ForegroundColor Yellow
    Write-Host 'machine but cannot change settings. Send the output above so this can be' -ForegroundColor Yellow
    Write-Host 'investigated.' -ForegroundColor Yellow
}
