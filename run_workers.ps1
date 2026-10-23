# Launch N translate.py workers in the background with auto-restart (Windows).
# Mirror of run_workers.sh for PowerShell.
#
# Usage:
#   .\run_workers.ps1                  # default: 10 workers
#   .\run_workers.ps1 -Workers 3       # custom worker count
#   .\run_workers.ps1 -Action stop     # kill all running workers
#   .\run_workers.ps1 -Action status   # show which workers are alive
#   .\run_workers.ps1 -Action tail     # live tail of all worker logs

[CmdletBinding()]
param(
    [int]$Workers = 10,
    [ValidateSet('start','stop','status','tail')]
    [string]$Action = 'start',
    # Optional HTTPS proxy (e.g. Bright Data rotating). When set, every worker
    # routes Google traffic through it, so per-IP rate limits don't apply.
    [string]$Proxy
)

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

$LogDir = Join-Path $PSScriptRoot 'logs'
$VenvPy = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

function Get-PidFiles {
    Get-ChildItem -Path $LogDir -Filter 'worker-*.pid' -ErrorAction SilentlyContinue
}

function Test-PidAlive {
    param([int]$ProcId)
    $null -ne (Get-Process -Id $ProcId -ErrorAction SilentlyContinue)
}

function Stop-Tree {
    param([int]$ProcId)
    Get-CimInstance Win32_Process -Filter "ParentProcessId=$ProcId" -ErrorAction SilentlyContinue |
        ForEach-Object { Stop-Tree -ProcId $_.ProcessId }
    try { Stop-Process -Id $ProcId -Force -ErrorAction Stop } catch {}
}

function Invoke-Stop {
    Write-Host "Stopping workers..."
    $files = Get-PidFiles
    if (-not $files) { Write-Host "  (no pid files found)"; return }
    foreach ($f in $files) {
        $procId = [int](Get-Content $f.FullName | Select-Object -First 1)
        if (Test-PidAlive -ProcId $procId) {
            Stop-Tree -ProcId $procId
            Write-Host "  killed $($f.Name) (pid $procId)"
        }
        Remove-Item $f.FullName -Force
    }
    Write-Host "Done."
}

function Invoke-Status {
    $alive = 0; $dead = 0
    $files = Get-PidFiles
    if (-not $files) { Write-Host "  (no pid files yet)"; return }
    foreach ($f in $files) {
        $procId = [int](Get-Content $f.FullName | Select-Object -First 1)
        if (Test-PidAlive -ProcId $procId) {
            Write-Host ("  ALIVE  {0}  (pid {1})" -f $f.Name, $procId)
            $alive++
        } else {
            Write-Host ("  DEAD   {0}  (pid {1})" -f $f.Name, $procId)
            $dead++
        }
    }
    Write-Host "---"
    Write-Host ("alive: {0}   dead: {1}" -f $alive, $dead)
}

function Invoke-Tail {
    $files = Get-ChildItem -Path $LogDir -Filter 'worker-*.log' -ErrorAction SilentlyContinue
    if (-not $files) { Write-Host "No log files yet in $LogDir"; return }
    Write-Host "Tailing $($files.Count) log(s). Press Ctrl+C to stop."
    Get-Content -Path ($files.FullName) -Tail 5 -Wait
}

function Invoke-Start {
    # Refuse to double-launch.
    foreach ($f in Get-PidFiles) {
        $procId = [int](Get-Content $f.FullName | Select-Object -First 1)
        if (Test-PidAlive -ProcId $procId) {
            Write-Error "Workers already running. Run '.\run_workers.ps1 -Action stop' first."
            exit 1
        }
        Remove-Item $f.FullName -Force
    }

    if (-not (Test-Path $VenvPy)) {
        Write-Error @"
.venv not found at $VenvPy

Create it first:
  py -3 -m venv .venv
  .\.venv\Scripts\python.exe -m pip install pymysql python-dotenv deep-translator

Then re-run: .\run_workers.ps1 -Workers $Workers
"@
        exit 1
    }

    # Sanity-check deps.
    & $VenvPy -c "import deep_translator, pymysql, dotenv" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Error ".venv exists but required packages are missing. Run: .\.venv\Scripts\python.exe -m pip install pymysql python-dotenv deep-translator"
        exit 1
    }

    Write-Host "Using venv: $VenvPy"
    if ($Proxy) {
        # Propagate to child processes so requests / deep_translator pick it up.
        $env:HTTPS_PROXY = $Proxy
        $env:HTTP_PROXY  = $Proxy
        $hostOnly = ($Proxy -replace '^https?://([^@]+@)?', '')
        Write-Host "Routing through proxy: $hostOnly"
    }
    Write-Host "Launching $Workers worker(s)..."

    $supervisor = Join-Path $PSScriptRoot '_worker_supervisor.ps1'
    if (-not (Test-Path $supervisor)) {
        Write-Error "Missing supervisor script: $supervisor"
        exit 1
    }

    for ($i = 0; $i -lt $Workers; $i++) {
        $logFile = Join-Path $LogDir "worker-$i.log"
        $pidFile = Join-Path $LogDir "worker-$i.pid"

        $proc = Start-Process -FilePath 'powershell.exe' `
            -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File', $supervisor, '-Worker', $i, '-Workers', $Workers) `
            -WindowStyle Hidden -PassThru

        $proc.Id | Out-File -FilePath $pidFile -Encoding ascii
        Write-Host ("  worker {0}  pid={1}  log={2}" -f $i, $proc.Id, $logFile)
    }

    Write-Host ""
    Write-Host "All $Workers workers launched. Safe to close this window."
    Write-Host "Check progress:  .\run_workers.ps1 -Action tail"
    Write-Host "Check status:    .\run_workers.ps1 -Action status"
    Write-Host "Stop everything: .\run_workers.ps1 -Action stop"
}

switch ($Action) {
    'start'  { Invoke-Start }
    'stop'   { Invoke-Stop }
    'status' { Invoke-Status }
    'tail'   { Invoke-Tail }
}
