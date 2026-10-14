# Capture the WMI traffic Dell Power Manager generates when the thermal mode changes.
#
# Why ETW: the thermal mode change was measured to write NOTHING to the registry,
# NOTHING to the filesystem, and NOTHING to the BIOS attribute interface. That
# leaves the WMI call itself as the only observable, and Microsoft-Windows-WMI-Activity
# is an ETW provider that records WMI operations.
#
# This is the runtime evidence static analysis cannot provide.
#
# Usage (elevated):
#   powershell -ExecutionPolicy Bypass -File .\scripts\trace_wmi_activity.ps1 -Start
#   ... change the thermal mode in Dell Power Manager ...
#   powershell -ExecutionPolicy Bypass -File .\scripts\trace_wmi_activity.ps1 -Stop
param(
    [switch]$Start,
    [switch]$Stop,
    [string]$Dir = "$env:USERPROFILE\Desktop\xps-wmi-trace"
)
$ErrorActionPreference = 'Continue'
New-Item -ItemType Directory -Force -Path $Dir | Out-Null
$etl = Join-Path $Dir 'wmi.etl'
$session = 'XpsHelperWmiTrace'

if ($Start) {
    Write-Host "=== starting WMI activity trace ==="
    # Clear any stale session first.
    logman stop $session -ets 2>&1 | Out-Null
    Remove-Item $etl -ErrorAction SilentlyContinue

    # Microsoft-Windows-WMI-Activity carries operation start/stop records.
    $r = logman start $session -p "Microsoft-Windows-WMI-Activity" 0xffffffffffffffff 5 -o $etl -ets 2>&1
    Write-Host ($r | Out-String).Trim()
    Write-Host ""
    Write-Host "NOW: open Dell Power Manager and change the thermal mode." -ForegroundColor Yellow
    Write-Host "Then run:  .\scripts\trace_wmi_activity.ps1 -Stop" -ForegroundColor Yellow
    exit 0
}

if ($Stop) {
    Write-Host "=== stopping trace ==="
    logman stop $session -ets 2>&1 | Out-Null
    Start-Sleep -Seconds 3
    if (-not (Test-Path $etl)) { Write-Host "no ETL produced at $etl"; exit 1 }
    Write-Host "ETL: $etl  ($((Get-Item $etl).Length) bytes)"
    Write-Host ""
    Write-Host "=== providers/events in the trace ==="
    $csv = Join-Path $Dir 'wmi.csv'
    tracerpt $etl -o $csv -of CSV -y 2>&1 | Out-Null
    if (Test-Path $csv) {
        # Look for anything naming the Dell/ACPI-WMI classes.
        Write-Host "--- lines mentioning our interfaces ---"
        Select-String -Path $csv -Pattern 'DoBFn|BDat|BFn|WMI_Query|WMI_Query|PNP0C14|root.wmi|SetUstt|ThermalManagement' |
            Select-Object -First 60 | ForEach-Object { Write-Host ("  " + $_.Line.Substring(0, [Math]::Min(300, $_.Line.Length))) }
        Write-Host ""
        Write-Host "--- total CSV lines: $((Get-Content $csv | Measure-Object -Line).Lines) ---"
    } else { Write-Host "tracerpt produced no CSV" }
    exit 0
}

Write-Host "usage: -Start   then change the mode   then -Stop"
