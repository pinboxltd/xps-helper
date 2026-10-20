<#
.SYNOPSIS
    Registers (or removes) the scheduled tasks that make XPS Helper start with Windows.

.DESCRIPTION
    A logon scheduled task is used rather than a Startup-folder shortcut for one
    specific reason: changing BIOS settings through cctk.exe requires
    administrator rights, and a Startup shortcut cannot request elevation
    without a UAC prompt on every login. A scheduled task can run "with highest
    privileges" silently.

    Tasks created:
      <TaskName>            Runs the tray GUI at logon, elevated.
      <TaskName> (Apply)    Optional: runs `xps-helper apply` when the
                            workstation is unlocked, to re-assert the saved
                            charge limit and thermal profile.

.PARAMETER GuiPath
    Full path to xps-helper-gui.exe. If omitted, the script looks for the
    PyInstaller output in ..\dist\xps-helper\ and then on PATH.

.PARAMETER CliPath
    Full path to xps-helper.exe, required only with -ApplyOnUnlock.

.PARAMETER ApplyOnUnlock
    Also create a task that re-applies saved settings when you unlock the
    machine. Useful because the EC can lose the charge-threshold setting across
    a full power loss.

.PARAMETER Uninstall
    Remove the tasks created by this script.

.EXAMPLE
    .\install-autostart.ps1 -GuiPath "C:\Tools\xps-helper.exe"

.EXAMPLE
    .\install-autostart.ps1 -GuiPath .\dist\xps-helper.exe -ApplyOnUnlock

.EXAMPLE
    .\install-autostart.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    # The single executable. Since v0.4.0 there is one file, not two: the same
    # binary opens the window with no arguments and runs the CLI with arguments.
    [string]$GuiPath,
    # Optional and normally unnecessary -- defaults to -GuiPath.
    [string]$CliPath,
    [string]$TaskName = 'XPS Helper',
    [switch]$ApplyOnUnlock,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

function Assert-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "This script must be run from an elevated PowerShell window " +
              "(right-click PowerShell -> Run as administrator)."
    }
}

function Resolve-Executable([string]$Explicit, [string]$FileName) {
    if ($Explicit) {
        $resolved = (Resolve-Path -LiteralPath $Explicit).Path
        if (-not (Test-Path -LiteralPath $resolved)) { throw "Not found: $Explicit" }
        return $resolved
    }
    # Look in the PyInstaller output next to this script's parent directory.
    $candidate = Join-Path (Split-Path -Parent $PSScriptRoot) "dist\xps-helper\$FileName"
    if (Test-Path -LiteralPath $candidate) { return (Resolve-Path -LiteralPath $candidate).Path }

    $onPath = Get-Command $FileName -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }

    throw "Could not find $FileName. Pass its full path with the -GuiPath/-CliPath parameter."
}

function Remove-XpsHelperTasks {
    foreach ($name in @($TaskName, "$TaskName (Apply)")) {
        $existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if ($existing) {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Host "Removed scheduled task: $name"
        }
    }
}

Assert-Administrator

if ($Uninstall) {
    Remove-XpsHelperTasks
    Write-Host "Done."
    exit 0
}

# Removing first makes the script idempotent instead of erroring on a re-run.
Remove-XpsHelperTasks | Out-Null

$guiExe = Resolve-Executable $GuiPath 'xps-helper-gui.exe'
Write-Host "GUI executable: $guiExe"

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Highest

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero)   # a tray app must not be killed after 3 days

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action (New-ScheduledTaskAction -Execute $guiExe) `
    -Trigger (New-ScheduledTaskTrigger -AtLogOn) `
    -Principal $principal `
    -Settings $settings `
    -Description 'Starts the XPS Helper tray app at logon, elevated so it can change BIOS settings.' | Out-Null
Write-Host "Registered scheduled task: $TaskName (at logon, highest privileges)"

if ($ApplyOnUnlock) {
    # One file since v0.4.0: the executable runs the CLI when given arguments, so
    # -CliPath only matters for an older two-file build.
    if (-not $CliPath) { $CliPath = $GuiPath }
    $cliExe = Resolve-Executable $CliPath 'xps-helper.exe'
    Write-Host "CLI executable: $cliExe"

    # An event trigger is the only way to run on unlock; the PowerShell cmdlets
    # have no -AtUnlock. Event 4801 is "workstation unlocked".
    $eventTrigger = New-CimInstance `
        -CimClass (Get-CimClass -Namespace Root/Microsoft/Windows/TaskScheduler `
                                -ClassName MSFT_TaskEventTrigger) `
        -ClientOnly
    $eventTrigger.Enabled = $true
    $eventTrigger.Subscription = @'
<QueryList><Query Id="0" Path="Microsoft-Windows-Winlogon/Operational"><Select Path="Microsoft-Windows-Winlogon/Operational">*[System[EventID=4801]]</Select></Query></QueryList>
'@

    Register-ScheduledTask `
        -TaskName "$TaskName (Apply)" `
        -Action (New-ScheduledTaskAction -Execute $cliExe -Argument 'apply') `
        -Trigger $eventTrigger `
        -Principal $principal `
        -Settings $settings `
        -Description 'Re-applies saved XPS Helper settings when the workstation is unlocked.' | Out-Null
    Write-Host "Registered scheduled task: $TaskName (Apply) (on workstation unlock)"
}

Write-Host ''
Write-Host 'Verify with:  Get-ScheduledTask -TaskName "XPS Helper*" | Format-Table TaskName,State'
Write-Host 'Remove with:  .\install-autostart.ps1 -Uninstall'
