# Schema of the BDat ACPI-WMI class -- the transport Dell uses for BIOS commands.
#
# Earlier probes reported "root/wmi has 0 classes" because __CLASS enumeration
# fails on ACPI-WMI classes (Invalid class). Querying BDat by name works, which is
# exactly what Dell's DAInterface does.
#
# READ-ONLY: this only inspects the class schema and instances. No DoBFn call is
# made here (see probe_bdat_invoke.ps1 for that, separately).
$ErrorActionPreference = 'Continue'

Write-Host "=== BDat class schema ==="
$cls = Get-CimClass -Namespace root/wmi -ClassName BDat
if (-not $cls) { Write-Host "  not found"; exit 1 }

Write-Host "  properties:"
foreach ($p in $cls.CimClassProperties) {
    Write-Host ("     {0,-22} {1}" -f $p.Name, $p.CimType)
}
Write-Host "  methods:"
foreach ($m in $cls.CimClassMethods) {
    $ps = ($m.Parameters | ForEach-Object {
        $dir = if ($_.Qualifiers | Where-Object { $_.Name -eq 'In' }) { 'in' } else { 'out' }
        "$($_.Name):$($_.CimType)/$dir"
    }) -join ', '
    Write-Host ("     {0}({1}) -> {2}" -f $m.Name, $ps, $m.ReturnType)
}

Write-Host ""
Write-Host "=== BDat instances ==="
$insts = Get-CimInstance -Namespace root/wmi -ClassName BDat -ErrorAction SilentlyContinue
if (-not $insts) { $insts = Get-WmiObject -Namespace root/wmi -Class BDat -ErrorAction SilentlyContinue }
if ($insts) {
    Write-Host "  count: $(@($insts).Count)"
    foreach ($i in @($insts)) {
        Write-Host "  --- InstanceName = $($i.InstanceName)"
        foreach ($p in @('BFn', 'BDat', 'Bytes', 'Data', 'Active', 'Vendor', 'Signature', 'Version', 'BufferLength')) {
            try {
                $v = $i.$p
                if ($v -is [byte[]]) {
                    Write-Host ("      {0} = byte[{1}] {2}" -f $p, $v.Length, (($v | Select-Object -First 32 | ForEach-Object { $_.ToString('x2') }) -join ' '))
                } elseif ($null -ne $v) {
                    Write-Host ("      {0} = {1}" -f $p, $v)
                }
            } catch { }
        }
    }
} else { Write-Host "  (no instances returned)" }

Write-Host ""
Write-Host "=== the exact instance name Dell hardcodes ==="
$exact = Get-CimInstance -Namespace root/wmi -ClassName BDat -Filter "InstanceName='ACPI\\PNP0C14\\0_0'" -ErrorAction SilentlyContinue
if ($exact) {
    Write-Host "  ACPI\PNP0C14\0_0 : FOUND"
    Write-Host "     InstanceName = $($exact.InstanceName)"
} else {
    Write-Host "  ACPI\PNP0C14\0_0 : not returned by that filter"
}
