<#
  Safety net for Mini PMC. Runs every 5 minutes (installed by install-service.ps1) and asks the program
  "are you alive and doing your cycles?" via http://127.0.0.1:<port>/healthz.
    - task not running       -> start it
    - running but unhealthy  -> restart it (only if it has been up for 10+ minutes, so a slow start is left alone)
  Writes what it did to logs\watchdog.log. Uses only what Windows already has.
#>
param([int]$Port = 8080)
$ErrorActionPreference = 'Stop'
$name = 'MiniPMC'
$root = Split-Path -Parent $PSScriptRoot
$log = Join-Path $root 'logs\watchdog.log'
function Note($m) {
    New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
    Add-Content -Path $log -Value ("{0:yyyy-MM-dd HH:mm:ss} {1}" -f (Get-Date), $m)
}

$task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
if (-not $task) { Note "task $name is not installed"; exit 1 }

if ($task.State -ne 'Running') {
    Note "task was $($task.State); starting it"
    Start-ScheduledTask -TaskName $name
    exit 0
}

$healthy = $false
try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/healthz" -UseBasicParsing -TimeoutSec 15
    $healthy = ($r.StatusCode -eq 200)
} catch { $healthy = $false }

if (-not $healthy) {
    $up = (Get-Date) - (Get-ScheduledTaskInfo -TaskName $name).LastRunTime
    if ($up.TotalMinutes -lt 10) { Note "unhealthy but only up $([int]$up.TotalMinutes) min; waiting"; exit 0 }
    Note "unhealthy after $([int]$up.TotalMinutes) min; restarting"
    Stop-ScheduledTask -TaskName $name
    Start-Sleep -Seconds 5
    Start-ScheduledTask -TaskName $name
}
