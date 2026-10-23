# Supervisor loop for a single translate.py worker.
# Invoked by run_workers.ps1; not meant to be run directly.
param(
    [Parameter(Mandatory=$true)][int]$Worker,
    [Parameter(Mandatory=$true)][int]$Workers
)

$ErrorActionPreference = 'Continue'
Set-Location -Path $PSScriptRoot

$env:PYTHONUNBUFFERED = '1'

$venvPy  = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$script  = Join-Path $PSScriptRoot 'src\translate.py'
$logFile = Join-Path $PSScriptRoot ("logs\worker-{0}.log" -f $Worker)

while ($true) {
    # cmd.exe handles the redirection so stderr/stdout land in the log without
    # PowerShell's NativeCommandError wrapping.
    cmd.exe /c "`"$venvPy`" -u `"$script`" --workers $Workers --worker $Worker >> `"$logFile`" 2>&1"

    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    "[$stamp] worker $Worker exited, restart in 30s" |
        Out-File -FilePath $logFile -Append -Encoding utf8

    Start-Sleep -Seconds 30
}
