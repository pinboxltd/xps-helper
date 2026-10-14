# Drive Dell's real DA command path: DataAbstractor.ExecuteDACommand(ref buffer).
#
# Why the low-level attempt was not enough: MakeBiosCall routes through
# DataAbstractor, which wraps the raw transport. Its ExecuteDACommand reads the
# response with GetOutputBuffer, which expects a DSCI-framed payload:
#     [4-byte length][payload][DSCI][payload][DSCI]...
# Calling root\wmi :: BFn.DoBFn directly therefore only ever returned an echo.
#
# This script reports the exact signatures first so the invocation is not guessed.
$ErrorActionPreference = 'Continue'

$dir = "$env:ProgramFiles\Dell\CommandPowerManager"
[AppDomain]::CurrentDomain.add_AssemblyResolve({
    param($s, $e)
    $simple = ($e.Name -split ',')[0]
    if ($simple -notmatch '^(SmbLib|ObjLib|SystemInterop|ComponentService|Utilities|Alerts|Clarity)$') { return $null }
    $p = Join-Path $dir ($simple + '.dll')
    if (Test-Path $p) { return [Reflection.Assembly]::LoadFrom($p) }
    return $null
})

$smb = [Reflection.Assembly]::LoadFrom((Join-Path $dir 'SmbLib.dll'))
$daT = $smb.GetType('Dell.CommandPowerManager.DataAbstraction.DataAbstractor')
$wdT = $smb.GetType('Dell.CommandPowerManager.Wmi.WmiDescriptor')
$siT = [Reflection.Assembly]::LoadFrom((Join-Path $dir 'SystemInterop.dll')).GetType('Dell.CommandPowerManager.SystemCalls.SystemInterop')

Write-Host "=== DataAbstractor ctor(s) ==="
foreach ($c in $daT.GetConstructors([Reflection.BindingFlags]::Public -bor [Reflection.BindingFlags]::NonPublic -bor [Reflection.BindingFlags]::Instance)) {
    Write-Host ("  ctor(" + ((($c.GetParameters()) | ForEach-Object { $_.ParameterType.Name + ' ' + $_.Name }) -join ', ') + ")")
}
Write-Host "=== DataAbstractor.ExecuteDACommand overload(s) ==="
foreach ($m in $daT.GetMethods([Reflection.BindingFlags]::Public -bor [Reflection.BindingFlags]::NonPublic -bor [Reflection.BindingFlags]::Instance) | Where-Object { $_.Name -eq 'ExecuteDACommand' }) {
    $parms = ($m.GetParameters() | ForEach-Object {
        $t = $_.ParameterType
        $n = $t.Name
        if ($t.IsByRef) { $n = $t.GetElementType().Name + '&' }
        "$n $($_.Name)"
    }) -join ', '
    Write-Host ("  {0} ExecuteDACommand({1})" -f $m.ReturnType.Name, $parms)
}
Write-Host "=== DataAbstractor.Initialize ==="
foreach ($m in $daT.GetMethods([Reflection.BindingFlags]::Public -bor [Reflection.BindingFlags]::NonPublic -bor [Reflection.BindingFlags]::Instance) | Where-Object { $_.Name -eq 'Initialize' }) {
    Write-Host ("  {0} Initialize()" -f $m.ReturnType.Name)
}
Write-Host "=== SystemInterop.GetOutputBuffer ==="
foreach ($m in $siT.GetMethods([Reflection.BindingFlags]::Public -bor [Reflection.BindingFlags]::NonPublic -bor [Reflection.BindingFlags]::Instance) | Where-Object { $_.Name -eq 'GetOutputBuffer' }) {
    $parms = ($m.GetParameters() | ForEach-Object { $_.ParameterType.Name + ' ' + $_.Name }) -join ', '
    Write-Host ("  {0} GetOutputBuffer({1})" -f $m.ReturnType.Name, $parms)
}
Write-Host "=== SystemInterop.GetSupportedDACIClasses / IsClassSupported ==="
foreach ($n in @('GetSupportedDACIClasses', 'IsClassSupported')) {
    foreach ($m in $siT.GetMethods([Reflection.BindingFlags]::Public -bor [Reflection.BindingFlags]::NonPublic -bor [Reflection.BindingFlags]::Instance) | Where-Object { $_.Name -eq $n }) {
        $parms = ($m.GetParameters() | ForEach-Object { $_.ParameterType.Name + ' ' + $_.Name }) -join ', '
        Write-Host ("  {0} {1}({2})" -f $m.ReturnType.Name, $n, $parms)
    }
}

Write-Host ""
Write-Host "=== building descriptor via DataAbstractor.Initialize() ==="
$da = [Activator]::CreateInstance($daT)
$initM = $daT.GetMethods([Reflection.BindingFlags]::Public -bor [Reflection.BindingFlags]::NonPublic -bor [Reflection.BindingFlags]::Instance) | Where-Object { $_.Name -eq 'Initialize' } | Select-Object -First 1
$wd = $initM.Invoke($da, @())
Write-Host ("  descriptor: Vendor={0} Signature='{1}' Version={2} BufferLength={3} IsInitialized={4}" -f `
    $wdT.GetProperty('Vendor').GetValue($wd), $wdT.GetProperty('Signature').GetValue($wd),
    $wdT.GetProperty('Version').GetValue($wd), $wdT.GetProperty('BufferLength').GetValue($wd),
    $wdT.GetProperty('IsInitialized').GetValue($wd))
