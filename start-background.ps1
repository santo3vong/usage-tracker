[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$runner = Join-Path $repo "run_server.py"
$runtime = Join-Path $repo "runtime"
$pidFile = Join-Path $runtime "server.pid"
$stdoutLog = Join-Path $runtime "server.out.log"
$stderrLog = Join-Path $runtime "server.err.log"
$url = "http://127.0.0.1:5050/"

function Test-CanonicalTrackerProcess {
    param(
        [int]$ProcessId,
        $ProcessInfo
    )

    if (-not $ProcessInfo) {
        return $false
    }

    # New managed launches use the absolute runner path and can be identified
    # directly from the command line.
    if ($ProcessInfo.CommandLine -like "*$runner*") {
        return $true
    }

    # Older launches used `-B run_server.py`, so the command line alone cannot
    # identify which checkout owns the listener.  Only accept that legacy form
    # when the loopback server is actually serving the exact runner file from
    # this checkout.
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

New-Item -ItemType Directory -Path $runtime -Force | Out-Null

$listener = Get-NetTCPConnection -LocalPort 5050 -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    $listenerPid = @($listener | Select-Object -ExpandProperty OwningProcess -Unique)[0]
    $listenerProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$listenerPid" -ErrorAction SilentlyContinue
    if (Test-CanonicalTrackerProcess -ProcessId $listenerPid -ProcessInfo $listenerProcess) {
        try {
            $existing = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2
            if ($existing.StatusCode -eq 200 -and $existing.Content -match "Usage Tracker") {
                Set-Content -LiteralPath $pidFile -Value $listenerPid -Encoding ascii
                Write-Output "Canonical Usage Tracker is already available at $url (PID $listenerPid)."
                exit 0
            }
        } catch {
            throw "The canonical Usage Tracker owns port 5050 but is not responding. Restart it with restart-server.ps1."
        }
    }
    throw "Port 5050 is occupied by PID $listenerPid from another checkout or application. It was not stopped."
}

$bundledPython = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$python = $null
$pythonArgs = @()
if (Test-Path -LiteralPath $bundledPython -PathType Leaf) {
    $python = $bundledPython
    $pythonArgs = @("-B", ('"' + $runner + '"'))
} else {
    $pyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        $python = $pyLauncher.Source
        $pythonArgs = @("-3", "-B", ('"' + $runner + '"'))
    } else {
        $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
        if (-not $pythonCommand) {
            throw "Python 3 was not found."
        }
        $python = $pythonCommand.Source
        $pythonArgs = @("-B", ('"' + $runner + '"'))
    }
}

$env:PYTHONIOENCODING = "utf-8"
$process = Start-Process -FilePath $python -ArgumentList $pythonArgs -WorkingDirectory $repo -WindowStyle Hidden -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog -PassThru
Set-Content -LiteralPath $pidFile -Value $process.Id -Encoding ascii

for ($attempt = 0; $attempt -lt 30; $attempt++) {
    if ($process.HasExited) {
        $details = if (Test-Path -LiteralPath $stderrLog) { (Get-Content -LiteralPath $stderrLog -Tail 20) -join [Environment]::NewLine } else { "No error log was created." }
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
        throw "Usage Tracker exited during startup.`n$details"
    }
    try {
        $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2
        if ($response.StatusCode -eq 200 -and $response.Content -match "Usage Tracker") {
            Write-Output "Usage Tracker started at $url (PID $($process.Id))."
            exit 0
        }
    } catch {
        Start-Sleep -Seconds 1
    }
}

Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
throw "Usage Tracker did not become ready at $url within 30 seconds."
