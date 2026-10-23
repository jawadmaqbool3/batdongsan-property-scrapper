# Print translate.py progress: %-done, pending, recent throughput.
#
# Usage:
#   .\progress.ps1            # one-shot snapshot
#   .\progress.ps1 -Watch     # refresh every 10s until Ctrl+C
#   .\progress.ps1 -Watch -Interval 30

[CmdletBinding()]
param(
    [switch]$Watch,
    [int]$Interval = 10
)

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

$venvPy = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    Write-Error ".venv not found at $venvPy"
    exit 1
}

$pyScript = Join-Path $PSScriptRoot '_progress.py'

function Show-Progress {
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Write-Host "--- $stamp ---"
    & $venvPy $pyScript
    if ($LASTEXITCODE -ne 0) { Write-Host "(python exited $LASTEXITCODE)" }
}

if ($Watch) {
    Write-Host "Watching every ${Interval}s. Ctrl+C to stop."
    while ($true) {
        Show-Progress
        Write-Host ""
        Start-Sleep -Seconds $Interval
    }
} else {
    Show-Progress
}
