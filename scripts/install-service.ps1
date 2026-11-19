<#
  Installs CloudOps Scheduler as a Windows Scheduled Task (named MiniPMC) that starts at boot (before anyone logs in), runs as SYSTEM,
  and restarts itself if it ever stops. Uses only what Windows already has - nothing to download.
  Run from an ADMINISTRATOR PowerShell:   .\scripts\install-service.ps1 [-Port 8080]
  HTTPS on 443 (PEM files, see docs):     .\scripts\install-service.ps1 -CertFile C:\mpmc\certs\minipmc.crt -KeyFile C:\mpmc\certs\minipmc.key
  Local-only: the service listens on 127.0.0.1 and no firewall port is opened (-OpenFirewall is accepted but ignored).
#>
param(
    [int]$Port = 0,
    [string]$BindHost = '127.0.0.1',
    [string]$CertFile = '',
    [string]$KeyFile = '',
    [switch]$OpenFirewall
)
$ErrorActionPreference = 'Stop'
$https = [bool]$CertFile
if ($https -and -not $KeyFile) { throw 'HTTPS needs both -CertFile and -KeyFile (PEM files).' }
if ($https -and -not (Test-Path $CertFile)) { throw "Not found: $CertFile" }
if ($https -and -not (Test-Path $KeyFile)) { throw "Not found: $KeyFile" }
if ($Port -eq 0) { $Port = if ($https) { 443 } else { 8080 } }
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
if ($https) { $arg += " --cert `"$CertFile`" --key `"$KeyFile`"" }
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
    -Description 'CloudOps Scheduler VM schedule manager' | Out-Null

if ($OpenFirewall) {
    Write-Host 'Note: -OpenFirewall ignored. CloudOps Scheduler is local-only (127.0.0.1); no inbound firewall rule is created.'
    Remove-NetFirewallRule -DisplayName 'Mini PMC' -ErrorAction SilentlyContinue  # rule name used by earlier installs
}
Start-ScheduledTask -TaskName $name

# Watchdog: every 5 minutes, start/restart the main task if it is down or not doing its cycles.
$wdName = 'MiniPMC-Watchdog'
$wdArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $root 'scripts\watchdog.ps1')`" -Port $Port"
if ($https) { $wdArgs += ' -Https' }
$wdAction  = New-ScheduledTaskAction -Execute 'powershell.exe' -WorkingDirectory $root -Argument $wdArgs
$wdTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$wdSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 3)
if (Get-ScheduledTask -TaskName $wdName -ErrorAction SilentlyContinue) { Unregister-ScheduledTask -TaskName $wdName -Confirm:$false }
Register-ScheduledTask -TaskName $wdName -Action $wdAction -Trigger $wdTrigger -Principal $principal -Settings $wdSettings `
    -Description 'Restarts CloudOps Scheduler if it is not running or not doing its cycles' | Out-Null
Start-Sleep -Seconds 4
Get-ScheduledTask -TaskName $name | Select-Object TaskName, State | Format-Table -AutoSize
Write-Host "Installed. Dashboard: $(if ($https) { 'https' } else { 'http' })://localhost:$Port   Logs: $root\logs\app.log"
Write-Host 'Stop: Stop-ScheduledTask MiniPMC   Start: Start-ScheduledTask MiniPMC   Remove: .\scripts\uninstall-service.ps1'
Write-Host 'After a code update (git pull): Stop-ScheduledTask MiniPMC; Start-ScheduledTask MiniPMC  (no reinstall needed).'
