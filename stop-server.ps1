[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$runner = (Resolve-Path -LiteralPath (Join-Path $repo "run_server.py")).Path
$pidFile = Join-Path $repo "runtime\server.pid"
$url = "http://127.0.0.1:5050/"

function Test-CanonicalTrackerProcess {
    param(
        [int]$ProcessId,
        $ProcessInfo
    )

    if (-not $ProcessInfo) {
        return $false
    }

    if ($ProcessInfo.CommandLine -like "*$runner*") {
        return $true
    }

    if ($ProcessInfo.CommandLine -notmatch '(?i)(?:^|\s)["'']?run_server\.py["'']?(?:\s|$)') {
        return $false
    }

    try {
        $remoteRunner = Invoke-WebRequest -Uri ($url + "run_server.py") -UseBasicParsing -TimeoutSec 2
        $localRunner = Get-Content -LiteralPath $runner -Raw
        return ($remoteRunner.StatusCode -eq 200 -and $remoteRunner.Content -ceq $localRunner)
    } catch {
        return $false
    }
}

if (-not (Test-Path -LiteralPath $pidFile -PathType Leaf)) {
    $listener = Get-NetTCPConnection -LocalPort 5050 -State Listen -ErrorAction SilentlyContinue
    if (-not $listener) {
        Write-Output "No managed Usage Tracker PID file or port 5050 listener was found."
        exit 0
    }
    $listenerPid = @($listener | Select-Object -ExpandProperty OwningProcess -Unique)[0]
    $listenerProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$listenerPid" -ErrorAction SilentlyContinue
    if (-not (Test-CanonicalTrackerProcess -ProcessId $listenerPid -ProcessInfo $listenerProcess)) {
        throw "Port 5050 is occupied by PID $listenerPid, but it cannot be verified as this Usage Tracker checkout. It was not stopped."
    }
    Set-Content -LiteralPath $pidFile -Value $listenerPid -Encoding ascii
    Write-Output "Recovered managed Usage Tracker PID $listenerPid from port 5050."
}

$serverPid = [int](Get-Content -LiteralPath $pidFile -Raw)
$process = Get-CimInstance Win32_Process -Filter "ProcessId=$serverPid" -ErrorAction SilentlyContinue
if (-not $process) {
    Remove-Item -LiteralPath $pidFile -Force
    Write-Output "The saved process is no longer running; stale PID file removed."
    exit 0
}

if (-not (Test-CanonicalTrackerProcess -ProcessId $serverPid -ProcessInfo $process)) {
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
