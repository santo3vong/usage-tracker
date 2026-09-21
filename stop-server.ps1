[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$runner = (Resolve-Path -LiteralPath (Join-Path $repo "run_server.py")).Path
$pidFile = Join-Path $repo "runtime\server.pid"
$port = 5051
$url = "http://127.0.0.1:$port/"

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

$savedPid = $null
if (Test-Path -LiteralPath $pidFile -PathType Leaf) {
    $rawPid = (Get-Content -LiteralPath $pidFile -Raw).Trim()
    if ($rawPid -match '^\d+$') {
        $savedPid = [int]$rawPid
    } else {
        Remove-Item -LiteralPath $pidFile -Force
        Write-Output "Invalid managed PID file removed."
    }
}

$listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
$listenerPid = if ($listener) { @($listener | Select-Object -ExpandProperty OwningProcess -Unique)[0] } else { $null }
$serverPid = $null
$process = $null

if ($listenerPid) {
    $listenerProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$listenerPid" -ErrorAction SilentlyContinue
    if (-not (Test-CanonicalTrackerProcess -ProcessId $listenerPid -ProcessInfo $listenerProcess)) {
        throw "Port $port is occupied by PID $listenerPid, but it cannot be verified as this Usage Tracker checkout. It was not stopped."
    }
    $serverPid = [int]$listenerPid
    $process = $listenerProcess
    if ($savedPid -ne $serverPid) {
        Set-Content -LiteralPath $pidFile -Value $serverPid -Encoding ascii
        Write-Output "Recovered managed Usage Tracker PID $serverPid from port $port."
    }
} elseif ($savedPid) {
    $savedProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$savedPid" -ErrorAction SilentlyContinue
    if ($savedProcess) {
        if (-not (Test-CanonicalTrackerProcess -ProcessId $savedPid -ProcessInfo $savedProcess)) {
            throw "PID $savedPid does not belong to this Usage Tracker checkout. It was not stopped."
        }
        $serverPid = $savedPid
        $process = $savedProcess
    } else {
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
        Write-Output "The saved process is no longer running; stale PID file removed."
    }
}

if (-not $serverPid -or -not $process) {
    Write-Output "No running Usage Tracker process for this checkout was found."
    exit 0
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
