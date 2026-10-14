# Capture the frame Dell Power Manager actually sends, by polling BDat.Bytes.
#
# BDat.Bytes is the interface buffer that DoBFn consumes. If the app polls it fast
# enough while Dell Power Manager performs a thermal-mode change, the content
# differs from an all-zero/our-frame state and reveals the true command format --
# the one thing static analysis could not give us.
#
# READ-ONLY: queries a property in a loop. Nothing is written.
param(
    [int]$Seconds = 120,
    [int]$IntervalMs = 5,
    [string]$OutFile = "$env:USERPROFILE\Desktop\xps-wmi-trace\bdat-poll.txt"
)
$ErrorActionPreference = 'Continue'
$sw = [Diagnostics.Stopwatch]::StartNew()
$seen = @{}
$samples = 0
$lastSig = ''

Write-Host "polling root\wmi :: BDat.Bytes for $Seconds s (every ${IntervalMs}ms)"
Write-Host "NOW change the thermal mode in Dell Power Manager." -ForegroundColor Yellow
Write-Host ""

while ($sw.Elapsed.TotalSeconds -lt $Seconds) {
    try {
        $inst = Get-CimInstance -Namespace root/wmi -ClassName BDat -ErrorAction Stop
        foreach ($i in @($inst)) {
            if (-not $i.Bytes) { continue }
            $b = $i.Bytes
            $samples++
            # Signature: length + non-zero count + a hash of the first 64 bytes, so
            # any change in the frame content is detectable without storing it all.
            $nz = 0
            foreach ($x in $b) { if ($x -ne 0) { $nz++ } }
            $head = ($b | Select-Object -First 64 | ForEach-Object { $_.ToString('x2') }) -join ''
            $sig = "$($b.Length)|$nz|$head"
            if ($sig -ne $lastSig) {
                $lastSig = $sig
                $stamp = [Math]::Round($sw.Elapsed.TotalSeconds, 2)
                if (-not $seen.ContainsKey($sig)) {
                    $seen[$sig] = $stamp
                    $line = "[$stamp s] len=$($b.Length) nonZero=$nz"
                    Write-Host $line
                    $hex = ($b | Select-Object -First 96 | ForEach-Object { $_.ToString('x2') }) -join ' '
                    Write-Host "    $hex"
                    Add-Content -Path $OutFile -Value $line
                    Add-Content -Path $OutFile -Value "    $hex"
                }
            }
        }
    } catch { }
    Start-Sleep -Milliseconds $IntervalMs
}

Write-Host ""
Write-Host "samples: $samples   distinct frames: $($seen.Count)"
Add-Content -Path $OutFile -Value "samples=$samples distinct=$($seen.Count)"
