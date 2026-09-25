# Windows-side watchdog: keep the rebocap dongle attached over USB/IP, self-healing.
# Re-attaches whenever COM3 is missing or not "OK" (a stale attach after the Frame's dongle
# re-enumerates shows Status "Unknown"), with backoff so a Frame-down window does not thrash.
param(
    [string]$Frame = '192.168.1.194',
    [string]$Busid = '1-1.4',
    [string]$Vid = '248A',
    [int]$IntervalSec = 3,
    [string]$LogPath = "$env:TEMP\rebocap-attach-watchdog.log"
)
$usbip = 'C:\Program Files\USBip\usbip.exe'
function Log($m) { "$([DateTime]::Now.ToString('HH:mm:ss')) $m" | Tee-Object -FilePath $LogPath -Append | Out-Null }
Log "watchdog start: frame=$Frame busid=$Busid"
$badStreak = 0
$backoff = 0
while ($true) {
    $dev = Get-PnpDevice -InstanceId "USB\VID_$Vid*" -ErrorAction SilentlyContinue | Select-Object -First 1
    $healthy = $dev -and ($dev.Status -eq 'OK')
    if ($healthy) {
        if ($badStreak -gt 0) { Log "recovered: COM device OK" }
        $badStreak = 0; $backoff = 0
    } else {
        $badStreak++
        $status = if ($dev) { $dev.Status } else { 'absent' }
        if ($badStreak -ge 2 -and $backoff -le 0) {
            Log "unhealthy (status=$status) -> detach+reattach"
            & $usbip detach -p 1 2>&1 | Out-Null
            Start-Sleep -Seconds 1
            $out = (& $usbip attach -r $Frame -b $Busid 2>&1) -join ' '
            Log "attach: $out"
            if ($out -match 'not connected|error') { $backoff = 5 } else { $backoff = 2 }
        } elseif ($backoff -gt 0) {
            $backoff--
        }
    }
    Start-Sleep -Seconds $IntervalSec
}
