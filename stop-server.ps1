[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$runner = (Resolve-Path -LiteralPath (Join-Path $repo "run_server.py")).Path
$pidFile = Join-Path $repo "runtime\server.pid"

if (-not (Test-Path -LiteralPath $pidFile -PathType Leaf)) {
    Write-Output "No managed Usage Tracker PID file was found."
    exit 0
}

$serverPid = [int](Get-Content -LiteralPath $pidFile -Raw)
$process = Get-CimInstance Win32_Process -Filter "ProcessId=$serverPid" -ErrorAction SilentlyContinue
if (-not $process) {
    Remove-Item -LiteralPath $pidFile -Force
    Write-Output "The saved process is no longer running; stale PID file removed."
    exit 0
}

if ($process.CommandLine -notlike "*$runner*") {
    throw "PID $serverPid does not belong to this Usage Tracker checkout. It was not stopped."
}

Stop-Process -Id $serverPid
for ($attempt = 0; $attempt -lt 20; $attempt++) {
    if (-not (Get-Process -Id $serverPid -ErrorAction SilentlyContinue)) {
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
        Write-Output "Usage Tracker PID $serverPid stopped."
        exit 0
    }
    Start-Sleep -Milliseconds 250
}

throw "Usage Tracker PID $serverPid did not stop within five seconds."
