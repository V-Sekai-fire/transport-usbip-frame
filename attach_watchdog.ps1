# Windows-side watchdog: keep every device the Frame exports attached over USB/IP, self-healing.
# The Frame's address, busids and client ports are rediscovered each pass, so a dock power-cycle
# (new busid) or a network move (new address) heals without editing anything.
param(
    [string[]]$Frame = @('192.168.1.194'),
    [int]$Port = 3240,
    [int]$IntervalSec = 3,
    [string]$LogPath = "$env:TEMP\usbip-frame-watchdog.log",
    [string]$StatePath = "$env:LOCALAPPDATA\usbip-frame\last-frame.txt"
)
$usbip = 'C:\Program Files\USBip\usbip.exe'
$mtx = New-Object System.Threading.Mutex($false, 'Global\usbip-frame-watchdog', [ref]$null)
if (-not $mtx.WaitOne(0)) { Write-Host 'another watchdog already running; exiting'; exit 0 }
function Log($m) { "$([DateTime]::Now.ToString('HH:mm:ss')) $m" | Tee-Object -FilePath $LogPath -Append | Out-Null }

function Candidates {
    $c = @()
    if (Test-Path $StatePath) { $c += Get-Content $StatePath -ErrorAction SilentlyContinue }
    $c += Get-NetIPConfiguration -ErrorAction SilentlyContinue |
        Where-Object { $_.NetAdapter.InterfaceDescription -match 'Valve' -and $_.IPv4DefaultGateway } |
        ForEach-Object { $_.IPv4DefaultGateway.NextHop }
    $c += $Frame
    $c | Where-Object { $_ } | Select-Object -Unique
}

function Imported {
    $out, $cur = @(), $null
    foreach ($l in (& $usbip port 2>&1)) {
        if ("$l" -match '^Port (\d+):') { $cur = @{ Port = [int]$Matches[1]; VidPid = ''; Remote = '' }; $out += $cur }
        elseif ($cur -and "$l" -match '\(([0-9a-f]{4}):([0-9a-f]{4})\)\s*$') { $cur.VidPid = "$($Matches[1]):$($Matches[2])" }
        elseif ($cur -and "$l" -match '-> usbip://(\S+)') { $cur.Remote = $Matches[1] }
    }
    $out
}

function Exported($h) {
    $txt = (& $usbip -t $Port list -r $h 2>&1) -join "`n"
    if ($LASTEXITCODE -ne 0) { return $null }
    @([regex]::Matches($txt, '(?m)^\s+(\d+-[\d.]+)\s+:.*\(([0-9a-f]{4}):([0-9a-f]{4})\)') |
        ForEach-Object { @{ Busid = $_.Groups[1].Value; VidPid = "$($_.Groups[2].Value):$($_.Groups[3].Value)" } })
}

function Healthy($vidpid) {
    $v, $p = $vidpid.ToUpper().Split(':')
    $dev = Get-PnpDevice -PresentOnly -InstanceId "USB\VID_$v&PID_$p*" -ErrorAction SilentlyContinue | Select-Object -First 1
    $dev -and $dev.Status -eq 'OK'
}

Log "watchdog start: candidates=$((Candidates) -join ',') port=$Port"
$bad = @{}
$wait = $IntervalSec
while ($true) {
    foreach ($i in Imported) {
        if (-not $i.VidPid -or (Healthy $i.VidPid)) { $bad.Remove($i.Port); continue }
        $bad[$i.Port] = 1 + [int]$bad[$i.Port]
        if ($bad[$i.Port] -ge 2) {
            Log "port $($i.Port) $($i.VidPid) from $($i.Remote) unhealthy -> detach"
            & $usbip detach -p $i.Port 2>&1 | Out-Null
            $bad.Remove($i.Port)
        }
    }
    $reached = $false
    foreach ($h in Candidates) {
        $devs = Exported $h
        if ($null -eq $devs) { continue }
        $reached = $true
        if (-not ((Test-Path $StatePath) -and (Get-Content $StatePath) -eq $h)) {
            New-Item -ItemType Directory -Force -Path (Split-Path $StatePath) | Out-Null
            Set-Content -Path $StatePath -Value $h
            Log "frame found at $h"
        }
        foreach ($d in $devs) {
            $out = (& $usbip -t $Port attach -r $h -b $d.Busid 2>&1) -join ' '
            Log "attach $($d.VidPid) at $h/$($d.Busid): $out"
        }
        break
    }
    $wait = if ($reached) { $IntervalSec } else { [Math]::Min(30, $wait * 2) }
    Start-Sleep -Seconds $wait
}
