<#
  Safety net for Mini PMC. Runs every 5 minutes (installed by install-service.ps1) and asks the program
  "are you alive and doing your cycles?" via http://127.0.0.1:<port>/healthz.
    - task not running       -> start it
    - running but unhealthy  -> restart it (only if it has been up for 10+ minutes, so a slow start is left alone)
  Writes what it did to logs\watchdog.log. Uses only what Windows already has.
#>
param([int]$Port = 8080, [switch]$Https)
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
    # The probe talks to itself on 127.0.0.1, so the certificate name cannot match: skip validation for this local check only.
    $scheme = 'http'
    if ($Https) {
        $scheme = 'https'
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls11
        # A compiled callback: a scriptblock callback fails on a thread without a PowerShell runspace.
        if (-not ('MiniPmcTrustAll' -as [type])) {
            Add-Type -TypeDefinition 'using System.Net; using System.Net.Security; using System.Security.Cryptography.X509Certificates; public static class MiniPmcTrustAll { public static void Enable() { ServicePointManager.ServerCertificateValidationCallback = delegate(object s, X509Certificate c, X509Chain ch, SslPolicyErrors e) { return true; }; } }'
        }
        [MiniPmcTrustAll]::Enable()
    }
    $r = Invoke-WebRequest -Uri "${scheme}://127.0.0.1:$Port/healthz" -UseBasicParsing -TimeoutSec 15
    $healthy = ($r.StatusCode -eq 200)
} catch { $healthy = $false; Note "probe error: $($_.Exception.Message)" }

if (-not $healthy) {
    $up = (Get-Date) - (Get-ScheduledTaskInfo -TaskName $name).LastRunTime
    if ($up.TotalMinutes -lt 10) { Note "unhealthy but only up $([int]$up.TotalMinutes) min; waiting"; exit 0 }
    Note "unhealthy after $([int]$up.TotalMinutes) min; restarting"
    Stop-ScheduledTask -TaskName $name
    Start-Sleep -Seconds 5
    Start-ScheduledTask -TaskName $name
}
