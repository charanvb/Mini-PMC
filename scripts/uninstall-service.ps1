#  Removes the Mini PMC scheduled task and firewall rule. Data, logs and the venv are left untouched.
$ErrorActionPreference = 'Stop'
if (Get-ScheduledTask -TaskName 'MiniPMC' -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName 'MiniPMC' -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName 'MiniPMC' -Confirm:$false
    Write-Host 'Task removed.'
} else { Write-Host 'Task was not installed.' }
Remove-NetFirewallRule -DisplayName 'Mini PMC' -ErrorAction SilentlyContinue
