<#
  Installs Mini PMC as a Windows Scheduled Task that starts at boot (before anyone logs in), runs as SYSTEM,
  and restarts itself if it ever stops. Uses only what Windows already has - nothing to download.
  Run from an ADMINISTRATOR PowerShell:   .\scripts\install-service.ps1 [-Port 8080] [-OpenFirewall]
#>
param(
    [int]$Port = 8080,
    [string]$BindHost = '0.0.0.0',
    [switch]$OpenFirewall
)
$ErrorActionPreference = 'Stop'
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not (New-Object Security.Principal.WindowsPrincipal $id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this from an Administrator PowerShell.'
}
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { throw "Not found: $py - create the venv first (see docs\VM-GUIDE.md)." }
if (-not (Test-Path (Join-Path $root 'data\minipmc.db'))) { throw 'Run "mpmc init" and create an admin user first.' }

$name = 'MiniPMC'
$arg = "-m minipmc --home `"$root`" serve --host $BindHost --port $Port"
$action    = New-ScheduledTaskAction -Execute $py -Argument $arg -WorkingDirectory $root
$trigger   = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew -StartWhenAvailable
if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
}
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description 'Mini PMC VM schedule manager' | Out-Null

if ($OpenFirewall) {
    Remove-NetFirewallRule -DisplayName 'Mini PMC' -ErrorAction SilentlyContinue
    New-NetFirewallRule -DisplayName 'Mini PMC' -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow | Out-Null
    Write-Host "Firewall: inbound TCP $Port allowed (Windows firewall only - also check the Azure NSG)."
}
Start-ScheduledTask -TaskName $name

# Watchdog: every 5 minutes, start/restart the main task if it is down or not doing its cycles.
$wdName = 'MiniPMC-Watchdog'
$wdAction  = New-ScheduledTaskAction -Execute 'powershell.exe' -WorkingDirectory $root `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $root 'scripts\watchdog.ps1')`" -Port $Port"
$wdTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$wdSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 3)
if (Get-ScheduledTask -TaskName $wdName -ErrorAction SilentlyContinue) { Unregister-ScheduledTask -TaskName $wdName -Confirm:$false }
Register-ScheduledTask -TaskName $wdName -Action $wdAction -Trigger $wdTrigger -Principal $principal -Settings $wdSettings `
    -Description 'Restarts Mini PMC if it is not running or not doing its cycles' | Out-Null
Start-Sleep -Seconds 4
Get-ScheduledTask -TaskName $name | Select-Object TaskName, State | Format-Table -AutoSize
Write-Host "Installed. Dashboard: http://localhost:$Port   Logs: $root\logs\app.log"
Write-Host 'Stop: Stop-ScheduledTask MiniPMC   Start: Start-ScheduledTask MiniPMC   Remove: .\scripts\uninstall-service.ps1'
Write-Host 'After a code update (git pull): Stop-ScheduledTask MiniPMC; Start-ScheduledTask MiniPMC  (no reinstall needed).'
