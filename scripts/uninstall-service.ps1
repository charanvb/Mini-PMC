#  Removes the CloudOps Scheduler scheduled tasks (MiniPMC, MiniPMC-Watchdog) and firewall rule. Data, logs and the venv are left untouched.
$ErrorActionPreference = 'Stop'
if (Get-ScheduledTask -TaskName 'MiniPMC' -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName 'MiniPMC' -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName 'MiniPMC' -Confirm:$false
    Write-Host 'Task removed.'
} else { Write-Host 'Task was not installed.' }
if (Get-ScheduledTask -TaskName 'MiniPMC-Watchdog' -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName 'MiniPMC-Watchdog' -Confirm:$false
    Write-Host 'Watchdog task removed.'
}
Remove-NetFirewallRule -DisplayName 'Mini PMC' -ErrorAction SilentlyContinue  # rule name used by earlier installs
