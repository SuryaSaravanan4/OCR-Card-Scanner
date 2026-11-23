<#
Connect the Pi to this PC's app over campus Wi-Fi and start the Pi's scanner.

The campus network blocks the Pi from reaching this PC directly, but allows SSH from
this PC to the Pi. So this opens one SSH session that:
  * carries a reverse tunnel (-R): on the Pi, http://127.0.0.1:8000 reaches the app
    running on THIS PC at 127.0.0.1:8000 (nothing is exposed on campus Wi-Fi);
  * runs pi/scan_agent.py on the Pi, which waits for "Scan stack" in the browser.
Close this window or press Ctrl+C to stop both; the Pi stops its servos when the
session ends.

Usage (from the repo folder, with the app already running in another window:
    python -m uvicorn app.main:app --port 8000):
    .\scripts\pi_link.ps1                         # real servos, Pi at the default address
    .\scripts\pi_link.ps1 -Mock                   # mock servos: nothing moves
    .\scripts\pi_link.ps1 -PiHost 172.30.226.199  # the Pi's address changed
    .\scripts\pi_link.ps1 -AgentArgs "--feed-s 1.2 --attempts 2"
Find the Pi's address on the Pi with: hostname -I
#>
param(
    [string]$PiHost = "172.30.226.152",
    [string]$PiUser = "pi-model-b",
    [int]$Port = 8000,
    [switch]$Mock,
    [string]$AgentArgs = ""
)

# The app must already be running here, or the Pi has nothing to talk to.
$app = Test-NetConnection -ComputerName 127.0.0.1 -Port $Port -InformationLevel Quiet -WarningAction SilentlyContinue
if (-not $app) {
    Write-Host "The app is not running on this PC (port $Port). Start it first in another window:" -ForegroundColor Yellow
    Write-Host "    python -m uvicorn app.main:app --port $Port"
    exit 1
}

$motor = if ($Mock) { "" } else { "--real-motor" }
$remote = "cd ~/OCR-Card-Scanner && ~/ocr-env/bin/python -u -m pi.scan_agent $motor --pc-url http://127.0.0.1:$Port $AgentArgs"

Write-Host "Connecting to $PiUser@$PiHost (servos: $(if ($Mock) { 'MOCK' } else { 'REAL' }))..."
Write-Host "Then open http://127.0.0.1:$Port/scan and click 'Scan stack'. Ctrl+C here stops the Pi."
# -t: a terminal on the Pi, so Ctrl+C and closing the window reach the scanner (servos stop).
# ExitOnForwardFailure: give up loudly if the tunnel can't be set up.
# ServerAlive*: notice a dead Wi-Fi link within ~45 s.
ssh -t -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 `
    -R "${Port}:127.0.0.1:${Port}" "$PiUser@$PiHost" $remote
