# Extract WMI operations from the ETW trace captured by trace_wmi_activity.ps1.
#
# Microsoft-Windows-WMI-Activity writes its payload under <UserData>, in a typed
# element whose name varies by record kind (Operation_New / Operation_Start /
# Operation_Complete / Operation_Failed / ...). Reading only EventData yields zero
# operations, which is why the first attempt found nothing.
#
# The fields that matter: Operation (the WMI call), NamespaceName, Query, Path,
# ClientProcessId. That is the runtime evidence for which call Dell Power Manager
# makes when the thermal mode changes.
param([string]$Dir = "$env:USERPROFILE\Desktop\xps-wmi-trace")

$ErrorActionPreference = 'Continue'
$xml = Join-Path $Dir 'wmi.xml'
if (-not (Test-Path $xml)) {
    Write-Host "no $xml - run trace_wmi_activity.ps1 -Start, change the mode, then -Stop"
    exit 1
}

$x = [xml](Get-Content $xml -Raw)
$events = @($x.Events.Event)
Write-Host "events: $($events.Count)"

function Get-Field {
    param($node, [string]$name)
    if (-not $node) { return $null }
    foreach ($c in $node.ChildNodes) {
        if ($c.LocalName -eq $name) { return $c.InnerText }
    }
    return $null
}

$rows = @()
foreach ($e in $events) {
    $ud = $e.UserData
    if (-not $ud -or -not $ud.HasChildNodes) { continue }
    foreach ($kind in $ud.ChildNodes) {
        $op = Get-Field $kind 'Operation'
        if (-not $op) { continue }
        $rows += [pscustomobject]@{
            Kind      = $kind.LocalName
            Id        = $e.System.EventID
            Op        = $op
            Namespace = Get-Field $kind 'NamespaceName'
            Query     = Get-Field $kind 'Query'
            Path      = Get-Field $kind 'Path'
            Class     = Get-Field $kind 'ClassName'
            ClientPid = Get-Field $kind 'ClientProcessId'
            User      = Get-Field $kind 'User'
        }
    }
}

Write-Host "records carrying an Operation: $($rows.Count)"
Write-Host ""

if ($rows.Count -eq 0) {
    Write-Host "no operations decoded; distinct UserData element names:"
    $events | ForEach-Object { $_.UserData.ChildNodes | ForEach-Object { $_.LocalName } } |
        Group-Object | Sort-Object Count -Descending | Select-Object -First 20 |
        ForEach-Object { Write-Host ("  {0,6}  {1}" -f $_.Count, $_.Name) }
    exit 1
}

Write-Host "=== distinct operations (top 30) ==="
$rows | Group-Object Op | Sort-Object Count -Descending | Select-Object -First 30 |
    ForEach-Object { Write-Host ("  {0,6}  {1}" -f $_.Count, $_.Name) }

Write-Host ""
Write-Host "=== anything touching the ACPI-WMI / BIOS interfaces ==="
$hits = $rows | Where-Object {
    ($_.Namespace -match 'root\\wmi|dcim|sysman') -or
    ($_.Path      -match 'PNP0C14|BDat|BFn') -or
    ($_.Op        -match 'BDat|BFn|DoBFn|WMI_Query') -or
    ($_.Query     -match 'BDat|BFn|WMI_Query|PNP0C14')
}
if ($hits) {
    $hits | Select-Object -First 60 | ForEach-Object {
        Write-Host ("  [{0}] {1}" -f $_.Id, $_.Op)
        if ($_.Namespace) { Write-Host ("       ns    : " + $_.Namespace) }
        if ($_.Path)      { Write-Host ("       path  : " + $_.Path) }
        if ($_.Query)     { Write-Host ("       query : " + $_.Query) }
        if ($_.ClientPid) { Write-Host ("       pid   : " + $_.ClientPid) }
    }
} else { Write-Host "  (none)" }

Write-Host ""
Write-Host "=== namespaces contacted ==="
$rows | Where-Object { $_.Namespace } | Group-Object Namespace | Sort-Object Count -Descending |
    Select-Object -First 20 | ForEach-Object { Write-Host ("  {0,6}  {1}" -f $_.Count, $_.Name) }
