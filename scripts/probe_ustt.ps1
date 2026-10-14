# Read the thermal mode via Dell's own managed interop, WITHOUT elevation.
#
# If GetUsttInformation works unelevated, the thermal interface is reachable
# without a UAC prompt and we can build on it. Read-only: nothing is set here.
$ErrorActionPreference = 'Continue'

$elev = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
Write-Host "elevated = $elev (want False)"

$dir = "$env:ProgramFiles\Dell\CommandPowerManager"
# ObjLib + its dependencies live here; register a resolver so LoadFrom resolves them.
$onResolve = {
    param($s, $e)
    $name = ($e.Name -split ',')[0] + '.dll'
    $p = Join-Path $dir $name
    if (Test-Path $p) { return [Reflection.Assembly]::LoadFrom($p) }
    return $null
}
[AppDomain]::CurrentDomain.add_AssemblyResolve($onResolve)

Write-Host ""
Write-Host "=== load SystemInterop ==="
try {
    $si = [Reflection.Assembly]::LoadFrom((Join-Path $dir 'SystemInterop.dll'))
    Write-Host "  loaded: $($si.FullName)"
} catch {
    Write-Host "  FAILED: $($_.Exception.Message)"; exit 1
}

$t = $si.GetType('Dell.CommandPowerManager.SystemCalls.SystemInterop')
Write-Host "  type: $($t.FullName)"

Write-Host ""
Write-Host "=== public members of interest ==="
$t.GetMembers([Reflection.BindingFlags]::Public -bor [Reflection.BindingFlags]::Instance -bor [Reflection.BindingFlags]::Static -bor [Reflection.BindingFlags]::DeclaredOnly) |
    Where-Object { $_.Name -match 'Ustt|AcpiWmi|WmiDescriptor|ServiceSetting|InterfaceBuffer|Smbios' } |
    ForEach-Object {
        if ($_ -is [Reflection.MethodInfo]) {
            $ps = ($_.GetParameters() | ForEach-Object { "$($_.ParameterType.Name) $($_.Name)" }) -join ', '
            Write-Host ("   METHOD {0} {1}({2})" -f $_.ReturnType.Name, $_.Name, $ps)
        } else {
            Write-Host ("   {0} {1}" -f $_.MemberType, $_.Name)
        }
    }

Write-Host ""
Write-Host "=== try IsAcpiWmiSupported (static?) ==="
$flags = [Reflection.BindingFlags]::Public -bor [Reflection.BindingFlags]::Instance -bor [Reflection.BindingFlags]::Static
foreach ($m in @('IsAcpiWmiSupported', 'get_WmiDescriptor', 'get_UseComponentService', 'get_InterfaceBufferSize')) {
    $mi = $null
    try { $mi = $t.GetMethod($m, $flags) } catch {}
    if (-not $mi) { Write-Host "   $m : not found"; continue }
    try {
        $target = $null
        if (-not $mi.IsStatic) {
            # need an instance
            $ctor = $t.GetConstructor([Type]::EmptyTypes)
            if ($ctor) { $target = $ctor.Invoke(@()) }
            else {
                $c2 = $t.GetConstructors() | Select-Object -First 1
                if ($c2) {
                    $args = $c2.GetParameters() | ForEach-Object { $null }
                    $target = $c2.Invoke($args)
                }
            }
        }
        $r = $mi.Invoke($target, @())
        Write-Host "   $m = $r"
    } catch {
        $inner = $_.Exception.InnerException
        $msg = if ($inner) { $inner.Message } else { $_.Exception.Message }
        Write-Host "   $m -> EXC $msg"
    }
}
