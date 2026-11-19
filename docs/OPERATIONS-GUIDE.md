# Mini PMC Operations Guide

This guide covers the Windows Server installation, dashboard operations, important CLI commands, updates, and troubleshooting. Examples use the production install folder `E:\Charan\Mini-PMC`; replace it if your installation is elsewhere.

## 1. Quick command reference

Open PowerShell and set up a helper. This keeps commands pointed at the installed application and its database:

```powershell
Set-Location E:\Charan\Mini-PMC
$MiniPmcHome = (Get-Location).Path
function pmc { & (Join-Path $MiniPmcHome ".venv\Scripts\python.exe") -m minipmc --home $MiniPmcHome @args }
```

Run `pmc --help` or `pmc <command> --help` for the installed version's options. Without the helper, use:

```powershell
.\.venv\Scripts\python.exe -m minipmc --home E:\Charan\Mini-PMC <command>
```

The global `--home` option goes **before** the subcommand. If it is omitted, Mini PMC uses the current directory and may open a different database.

### Windows task and health

```powershell
Get-ScheduledTask MiniPMC
Get-ScheduledTaskInfo MiniPMC | Select-Object LastRunTime, LastTaskResult
Get-ScheduledTaskInfo MiniPMC-Watchdog | Select-Object LastRunTime, LastTaskResult
Stop-ScheduledTask MiniPMC
Start-ScheduledTask MiniPMC
```

### Scheduler, modes, and checks

```powershell
pmc mode                                  # show current mode
pmc mode readonly                         # observe only
pmc mode dryrun                           # decide and audit, do not act
pmc mode pilot --vms VM-ONE,VM-TWO        # operate only on this pilot list
pmc mode live                             # prompts for the exact word LIVE
pmc pause                                 # stop scheduled reconciliation
pmc resume
pmc desired VM-NAME --actual running      # explain the current schedule decision
pmc desired VM-NAME --actual stopped
pmc desired VM-NAME --actual running --at 2026-12-01T04:30:00Z
pmc validate
pmc creds-check
pmc creds-check --vm VM-NAME              # read-only connection and VM-state checks
pmc cycle                                 # run one immediate cycle and one queued-action wave
```

**`pmc cycle` can start or stop real VMs** when the current mode is Pilot or Live. Check the mode, pause state, and target list first. A regular scheduler tick runs every five minutes; a schedule is planned once per 15-minute UTC slot. Running `cycle` again in the same slot can read status and send one queue wave, but it does not plan that slot again. The queue sends at most one configured wave (normally 100 operations); the background pump sends later waves at its configured interval. Do not run this CLI command against the production home while the `MiniPMC` task is running: it starts a separate scheduler engine and may overlap the background scheduler. To restart the production app, stop and start the Scheduled Task instead.

### VMs, schedules, overrides, and exports

```powershell
pmc vm list
pmc vm list --find "search text"
pmc vm list --unmanaged
pmc vm show VM-NAME
pmc vm add VM-NAME --provider azure --scope <subscription-id> --group <resource-group> --schedule "SCHEDULE NAME"
pmc vm add VM-NAME --provider gcp --scope <project-id> --group <zone> --schedule "SCHEDULE NAME"
pmc vm enable VM-NAME
pmc vm disable VM-NAME
pmc vm set-schedule VM-NAME "SCHEDULE NAME"
pmc vm set-schedule VM-NAME -
pmc vm remove VM-NAME

pmc schedule list
pmc schedule show "SCHEDULE NAME"
pmc schedule test "SCHEDULE NAME"
pmc schedule test "SCHEDULE NAME" --at 2026-03-29T00:30:00Z
pmc schedule add "<schedule text>" --name "NEW NAME"
pmc schedule remove "SCHEDULE NAME"

pmc override list
pmc override set VM-NAME --days 1 --reason "maintenance"
pmc override set VM-NAME --until 2026-12-31T18:00:00Z --reason "approved exception"
pmc override set VM-NAME --forever --reason "manual control"
pmc override clear VM-NAME

pmc import --vms inventory.csv                 # preview only
pmc import --vms inventory.csv --apply
pmc export --out resources.csv                 # VM list as CSV
```

CLI override timestamps are UTC; append `Z` to an ISO timestamp. `--days` is a duration from now. The dashboard's manual-action override dialog labels its date/time as IST. In the dashboard, the "Until someone clears it" option is offered to Global admins only; CloudOps admins can set an override for a number of hours or until an IST date/time. An active override makes the scheduler skip that VM until the override expires or is cleared.

Schedule deletion is restricted to Global admins in the dashboard. Built-in schedules cannot be deleted; schedules with attached VMs cannot be deleted until those VMs are moved to another schedule. CLI deletion accepts one schedule name at a time.

### Users and signals

```powershell
pmc user list
pmc user add USERNAME --role admin       # prompts securely for a password
pmc user add USERNAME --role cloudops
pmc user add USERNAME --role reader
pmc user passwd USERNAME                 # prompts securely
pmc user role USERNAME cloudops
pmc user enable USERNAME
pmc user disable USERNAME
pmc user remove USERNAME
pmc signal-test --vm VM-NAME             # displays the signal only; does not send
pmc signal-test --vm VM-NAME --action block --send  # sends a real Logic App request
```

Avoid `--password` on the command line: it can be exposed in process listings or shell history. Prefer the secure prompt. A password set for a user in the dashboard is one-time; the user must change it at first login.

### Certificates and manual dashboard

```powershell
pmc make-cert --names "server-hostname,server-fqdn,server-ip"
pmc pfx-to-pem C:\path\server.pfx --out E:\Charan\Mini-PMC\certs
pmc serve --host 127.0.0.1 --port 8080
pmc serve --host 0.0.0.0 --port 443 --cert certs\minipmc.crt --key certs\minipmc.key
```

The normal production deployment is the `MiniPMC` Scheduled Task, not a manually run `serve` process. Do not start a second server on the same port.

## 2. First-time setup

1. Follow the [VM and cloud setup guide](./VM-GUIDE-UI-AND-CLOUD.md) for installing Python dependencies, configuring the Azure managed identity, GCP service account, Key Vault, and Logic Apps. Treat Logic App URLs and private keys as credentials.
2. From an elevated PowerShell, move to the install folder and create the private settings file if it does not already exist:

   ```powershell
   Set-Location E:\Charan\Mini-PMC
   Copy-Item config\settings.example.json config\settings.json
   notepad config\settings.json
   ```

   Fill in the required identity, Key Vault, and Logic App settings. Do not overwrite an existing `settings.json`; it may contain production configuration.
3. Initialize the database and check configuration. Create the first dashboard administrator; the CLI prompts for the password without echoing it:

   ```powershell
   .\.venv\Scripts\python.exe -m minipmc --home E:\Charan\Mini-PMC init
   .\.venv\Scripts\python.exe -m minipmc --home E:\Charan\Mini-PMC validate
   .\.venv\Scripts\python.exe -m minipmc --home E:\Charan\Mini-PMC user add admin --role admin
   .\.venv\Scripts\python.exe -m minipmc --home E:\Charan\Mini-PMC creds-check
   ```

   Resolve all `ERROR` results before proceeding. `creds-check --vm VM-NAME` can additionally test reading one onboarded VM; it does not start or stop it.
4. Choose the access mode. For team access over HTTPS on port 443, use a certificate whose names match the hostname/IP people will use. A self-signed certificate is acceptable for initial use, but each user's device must trust it to avoid browser warnings. Run the installer from an Administrator PowerShell:

   ```powershell
   .\scripts\install-service.ps1 -CertFile "E:\Charan\Mini-PMC\certs\minipmc.crt" -KeyFile "E:\Charan\Mini-PMC\certs\minipmc.key" -OpenFirewall
   ```

   The installer creates the `MiniPMC` startup task and the `MiniPMC-Watchdog` five-minute task. Limit inbound TCP 443 in the Windows Firewall and Azure NSG to approved office/VPN sources. The installer does not configure the Azure NSG.

## 3. Daily dashboard operation

- Open `https://<server-name>/` and log in. A server restart logs users out because web sessions are held in memory; the default session expires after 12 hours. Five failed logins from the same IP trigger a one-minute lockout.
- **Reader** can view resources, schedules, overrides, and audit events, and export resource/schedule spreadsheets.
- **CloudOps admin** can manage VMs and schedules, start/stop VMs, manage timed overrides (hours or until an IST date/time), send reports, export, and view the audit log. They cannot set an override that lasts until someone clears it, change modes, pause the scheduler, manage users/connections/email settings, or delete VMs/schedules.
- **Global admin** can perform all actions, including "until someone clears it" overrides, configuration, user management, and deletion.
- Onboard VMs with the columns `VM name, provider, project/subscription ID, zone/resource group, schedule`. Paste one VM per line or upload a CSV. Blank or `none` schedule means unmanaged; ensure the schedule is in column five. Large one-time imports can take time.
- Refresh status to read current cloud power states. Scheduled actions reconcile desired state against the most recent status. For manual Start/Stop, use the action dialog and review any override prompt before confirming.
- In **Schedules**, use schedule time checks to confirm the selected time zone and expected state. Export resources/schedules to Excel from their respective tabs. Use the Audit Log to investigate actions and download its CSV.
- Configure **Settings > Email & alerts** with recipients and test email before relying on alerts or reports. Automatic reports run at the configured daily time and time zone; operational alerts are sent for selected failures and are throttled to at most one email per alert key per hour.

### Schedule modes

| Mode | Behavior |
| --- | --- |
| Read-only | Reads and records status; no scheduled start/stop actions. |
| Dry run | Calculates and audits planned actions; does not send cloud actions. |
| Pilot | Performs actions only for VMs in the pilot list. |
| Live | Performs scheduled actions for all eligible managed VMs. |

Roll out in order: Read-only, Dry run, Pilot on a small verified list, then Live. Changing mode clears the current slot marker so the scheduler can plan under the new mode. Pause stops scheduled reconciliation; manual Start/Stop can still be submitted when the mode allows it.

## 4. Updating code and backing up data

For ordinary code or UI changes, update the code in the install folder, then restart the task. Reinstalling the task is not needed unless changing its port, bind address, HTTPS certificate/key paths, or task installation:

```powershell
Set-Location E:\Charan\Mini-PMC
git pull
.\.venv\Scripts\python.exe -m pytest tests -q
Stop-ScheduledTask MiniPMC
Start-ScheduledTask MiniPMC
```

Use the project's approved deployment process instead of `git pull` if production updates are staged or packaged separately. If `requirements.txt` changed, install the updated dependencies into the task's virtual environment before restarting. After deployment, use **Ctrl+F5** in the browser to reload changed JavaScript/CSS.

Restarting the task will log out all users. Wait for the task to be Running, then verify `https://localhost/healthz` on the server and open the dashboard. If changing HTTPS certificate, port, or bind host, rerun `scripts\install-service.ps1` with the new options so both the service task and watchdog use the updated settings.

Back up the SQLite database using SQLite's online backup API (safe while the app is running). From the repository root:

```powershell
$backup = "data\minipmc-$((Get-Date).ToString('yyyyMMdd-HHmmss')).db"
python -c "import sqlite3,sys; s=sqlite3.connect('data/minipmc.db'); d=sqlite3.connect(sys.argv[1]); s.backup(d); d.close(); s.close()" $backup
```

Keep the backup in an approved protected location. `config\settings.json`, certificates/private keys, and any credential files also require secure backup handling; do not commit them. Never delete `data\` as a troubleshooting step.

## 5. Scheduler and queue behavior

- The background scheduler ticks every five minutes. It refreshes cloud state, finalizes pending operations, and plans eligible actions.
- A slot is planned once per 15-minute UTC interval. The `last_slot` marker and queued scheduled actions are committed together.
- The pump sends queued actions in waves (defaults: 100 VMs per minute, 10 workers). Manual actions are prioritized and can bypass the wait for the next scheduled wave.
- Before stopping, Mini PMC sends the configured block signal; before starting, it sends unblock. Signal sending is fire-and-forget and does not hold up the cloud operation. A successful Logic App HTTP response means accepted, not that its downstream email/workflow completed.
- `max_actions_per_slot` is a safety cap (default 2500). If exceeded, the slot is capped and no scheduled actions are queued; inspect schedules before increasing it.
- The host guard prevents Mini PMC from stopping the machine it runs on. Do not remove this protection.
- `/healthz` reports scheduler heartbeat health, not the health of every cloud API, VM, or email workflow. The watchdog checks this endpoint every five minutes and logs only unhealthy/probe/restart events; no new watchdog log line usually means the probe succeeded.

## 6. Logs and audit events

Paths are relative to `E:\Charan\Mini-PMC`:

| Path | Contents |
| --- | --- |
| `logs\app.log` | Application and scheduler cycle messages. |
| `logs\error.log` | Warnings and errors. Both application logs rotate at 5 MB with up to 10 backups. |
| `logs\watchdog.log` | Watchdog probe errors, task starts, and restart decisions. Silence is normal when healthy. |
| `logs\audit\YYYY-MM-DD.jsonl` | Daily machine-readable audit events (UTC date). |
| Dashboard > Audit Log | Filterable event history and CSV export. |

Useful audit actions include `ACTIONS_QUEUED`, `START_REQUESTED`, `STOP_REQUESTED`, `ACTION_CONFIRMED`, `ACTION_FAILED`, `ACTION_STUCK`, `ACTION_SKIPPED`, `ACTION_CAP_EXCEEDED`, `SIGNAL_SENT`, `SIGNAL_FAILED`, `DRYRUN_SUMMARY`, `OVERRIDE_SET`, `OVERRIDE_CLEARED`, `REPORT_SENT`, `REPORT_FAILED`, `ALERT_SENT`, `ALERT_FAILED`, `SCHEDULER_RESTARTED`, `LOGIN_OK`, and `LOGIN_FAILED`.

Quick log checks:

```powershell
Get-Content logs\app.log -Tail 100
Get-Content logs\error.log -Tail 100
Get-Content logs\watchdog.log -Tail 30
Get-ChildItem logs\audit | Sort-Object LastWriteTime -Descending | Select-Object -First 5
```

## 7. Troubleshooting

| Symptom | Check and next step |
| --- | --- |
| Scheduler did not start/stop a VM | Check dashboard mode is Pilot/Live, scheduler is not paused, VM is enabled and has a schedule, no active override exists, and Pilot includes the VM. Run `pmc desired VM-NAME --actual <running\|stopped>` and inspect its reason. Check `Get-ScheduledTask MiniPMC`, `logs\app.log`, and Audit Log. Planning only happens once per 15-minute UTC slot; `pmc cycle` sends at most one queue wave and may not re-plan a slot already handled. |
| `desired` says `STATUS_UNKNOWN_OR_STALE` | Refresh status in Resources; run `pmc creds-check --vm VM-NAME`; inspect `logs\error.log` and the VM's provider permissions/network. The scheduler skips stale or unknown state rather than risking an incorrect action. |
| `validate` reports time zones cannot be loaded | Install the project's declared dependencies in its virtual environment: `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`, then rerun `pmc validate`. |
| Signed out after an update/restart | Expected: sessions are in memory. Log in again; there is no need to recreate the account. |
| Watchdog says unhealthy/restarting | Check `Get-ScheduledTaskInfo MiniPMC` and `MiniPMC-Watchdog`, `logs\watchdog.log`, `logs\error.log`, and `Invoke-WebRequest https://localhost/healthz -UseBasicParsing`. If the task has been up for less than 10 minutes, the watchdog waits before restarting it. The local watchdog deliberately ignores certificate validation for its loopback probe; a browser's certificate-name/trust warning is a separate certificate issue. |
| Watchdog log has no recent lines | Usually healthy: it records unhealthy probes and restarts, not successful probes. Check `/healthz` and task state if uncertain. |
| `sqlite3.IntegrityError: FOREIGN KEY constraint failed` while saving VM status | Update to the latest code and restart. Status saving now skips VMs removed during a refresh. If it recurs, capture the complete traceback, time, and matching `logs\app.log`/`logs\error.log`; do not delete the database. |
| Bulk onboarding seems slow | Large onboarding is a one-time database operation and may take time. Keep the page open, avoid submitting the same batch repeatedly, then check Resources for the VM count and errors. |
| Cloud API returns 429 / throttling | Mini PMC retries HTTP 429 using `Retry-After` when supplied. Check cloud/provider quotas and permissions; waves and worker count control action throughput. Persistent throttling needs provider-side quota or pacing review. |
| Provider list seems incomplete | The application stops pagination at its configured page limit and reports if more pages remain. Check the error log and provider account scope/permissions before assuming the inventory is complete. |
| Report email or alert not received | Verify **Settings > Email & alerts** recipients and run **Send test email**. Check `creds-check`, `ALERT_NOT_SENT`, `ALERT_FAILED`, `REPORT_FAILED`, and `logs\error.log`. Confirm the email Logic App accepts `ToEmail`, `Subject`, `Message`, and optional base64 `Attachments`; check its run history and Office 365 connection. Alerts are throttled by key for one hour. |
| HTTPS fails or browser warns about certificate | Verify the certificate/key files are PEM, include the hostname users enter, and the task is configured with those paths. Check the listener using `Get-NetTCPConnection -LocalPort 443 -State Listen`. Trust the self-signed certificate on client machines or use a corporate CA certificate. Check Windows Firewall and Azure NSG rules. |
| Failed logins or one-minute lockout | Check `logs\error.log` for the username and source IP. Five failed attempts from one IP lock that IP out for 60 seconds. Confirm the user is enabled and has the correct password; an admin can reset it using **Users** or `pmc user passwd USERNAME`. |
| First login cannot open other pages | A one-time password requires the user to set a new password first. Complete the password-change prompt. |
| Action is `STUCK` or `FAILED` | Check the action's audit details, `logs\error.log`, provider permissions/quota, and current VM state. `STUCK` means the requested state was not confirmed within the configured timeout; inspect the cloud provider before retrying manually. |
| Schedule deletion is blocked | Move every VM using that schedule to another schedule first. Built-in schedules cannot be deleted. |

### Health probe from PowerShell

On the server, use:

```powershell
Invoke-WebRequest https://localhost/healthz -UseBasicParsing
```

Expected response: HTTP 200 and JSON with `"ok": true`. If PowerShell reports a certificate name/trust error, test using the hostname present in the certificate. Do not disable certificate validation globally as a workaround.
