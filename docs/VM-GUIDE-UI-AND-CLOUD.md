# Mini PMC - dashboard, keep-running, Azure identity and GCP key

Run everything from the repo folder on the VM (`E:\Charan\Mini-PMC`) in PowerShell, venv active.

## 1. Pull and test
```
git pull
python -m pip install -r requirements-dev.txt
python -m pytest tests -q
```
No new packages are needed (the web server, Azure and GCP calls use only the standard library).

## 2. Dashboard
```
python -m minipmc user add admin --role admin      # prompts for password (min 8 chars)
python -m minipmc user add team --role reader      # reader = look only; admin = everything
python -m minipmc serve --port 8080                # test run, Ctrl+C to stop
```
Open http://localhost:8080. Roles: reader (look only), admin (everything). Existing operator/viewer users are converted automatically (operator becomes admin, viewer becomes reader) the next time `python -m minipmc init` runs.
There is no TLS yet: use it on the internal network only.

## 3. Keep it running (survives reboot and crashes)
Elevated PowerShell:
```
.\scripts\install-service.ps1 -Port 8080 -OpenFirewall
Get-ScheduledTask MiniPMC
```
It runs as SYSTEM at boot using the repo's `.venv`, restarts every minute if it dies. Logs: `logs\app.log`.
After `git pull`: `Stop-ScheduledTask MiniPMC; Start-ScheduledTask MiniPMC`.
Remove: `.\scripts\uninstall-service.ps1`. Also open the port in the Azure NSG if browsing from another machine.
Do not keep a `serve` running in a terminal at the same time (port clash).

## 4. Azure: user-assigned managed identity (UAMI)
1. Use the existing **CloudBAU-Automation** user-assigned managed identity and copy its **Client ID**.
2. VM `bnlwefspap20560` > Identity > User assigned > ensure `CloudBAU-Automation` is added.
3. Key Vault > Access control (IAM) > give `CloudBAU-Automation` the role **Key Vault Secrets User**.
   If the vault has a firewall, allow this VM's network.
4. Subscriptions/resource groups holding the VMs > IAM > give `CloudBAU-Automation` **Reader** now
   (later a role allowing `Microsoft.Compute/virtualMachines/start/action`, `deallocate/action`, `read`).

## 5. GCP: JSON key in Key Vault
1. Create a GCP service account with permissions `compute.instances.get/list/start/stop` on the projects.
2. Create a JSON key, then store it:
   ```
   az keyvault secret set --vault-name <vault> --name gcp-sa-key --file key.json
   ```
   (or paste the contents as a secret in the portal). **Delete key.json afterwards.**
3. Settings:
   ```
   copy config\settings.example.json config\settings.json
   notepad config\settings.json     # uami_client_id, key_vault_url, key_secret_name, logic_app
   ```
   `settings.json` is git-ignored.

## 6. Logic App URL and Omnibus signals

In the Logic App trigger, copy the complete **HTTP POST URL**, including its `sig=` query parameter. Treat it
like a credential: do not paste it into GitHub, a tracked example file, or logs.

Recommended: create a Key Vault secret named `logicapp-omnibus-url` with that full URL. Set the following in
the git-ignored `config\settings.json`:

```json
"logic_app": {
  "url_secret_name": "logicapp-omnibus-url",
  "timeout_seconds": 30,
  "retries": 3
}
```

`CloudBAU-Automation` must have **Key Vault Secrets User** on the vault. For a temporary test only, you can set
`"url": "<full HTTP POST URL>"` instead of `url_secret_name`; the Key Vault option is preferred.

Mini PMC sends the Logic App `{"vms":[{"name":"...","project":"...","zone":"..."}],"action":"block"}` before
stopping, or `"unblock"` before starting. `project` is the Azure subscription ID or GCP project ID; `zone` is
the Azure resource group or GCP zone. Mini PMC does not poll the workflow: a 2xx response means the Logic App
accepted the request. **Settings > Connections** can check the URL configuration and send a manually confirmed
test signal; that sends a real Omnibus signal, so use it only when appropriate.

## 7. Check
```
python -m minipmc creds-check --vm <azure-vm-name>   # Azure VM: only the managed identity + that VM
python -m minipmc creds-check --vm <gcp-vm-name>     # GCP VM: also Key Vault key + Google token
```
This only reads; it never starts or stops anything. Paste the output back.

## 8. Time zones - how to check
```
python -m minipmc schedule test "<schedule name>"
python -m minipmc schedule test "<schedule name>" --at 2026-03-29T00:30:00Z
```
Shows the same instant in UTC, London (BST/GMT) and the schedule's own zone, what state is expected, and the next
switches. The Schedules tab in the dashboard shows the same under "Time check".

## 9. Data
VMs, overrides and users live in `data\minipmc.db` (git-ignored). `git pull`, restarts and reboots never touch it.
Only deleting `data\` (or `vm remove`) removes data. Back it up by copying `data\minipmc.db`; `python -m minipmc export --out vms.csv` writes the VM list.

## 10. Team access over HTTPS (port 443), Start/Stop buttons, bulk onboarding

**HTTPS on 443** (certificate must be PEM: a `.crt` with the chain and a `.key`):
1. Get a certificate for the name people will type (corporate CA preferred). Have a `.pfx`? `python -m minipmc pfx-to-pem C:\path\cert.pfx --password xxx` writes `certs\minipmc.crt` and `certs\minipmc.key`. No certificate? `pip install cryptography`, then `python -m minipmc make-cert --names "bnlwefspap20560,bnlwefspap20560.yourdomain,10.x.x.x"` (self-signed: browsers warn until the .crt is trusted on each PC).
2. Make sure nothing else uses 443 (`netstat -ano | findstr :443`; IIS/other web servers must be stopped or moved).
3. Re-run once from an Administrator PowerShell: `.\scripts\install-service.ps1 -CertFile "<root>\certs\minipmc.crt" -KeyFile "<root>\certs\minipmc.key" -OpenFirewall` (port defaults to 443 with a certificate; this also opens the Windows firewall and points the watchdog at https).
4. Azure: allow inbound TCP 443 in the VM's NSG, from your office/VPN ranges only.
5. After that, code updates still only need `Stop-ScheduledTask MiniPMC; Start-ScheduledTask MiniPMC`. Renew a certificate by replacing the two files and restarting the task.

**Team members:** Users tab > Add user. A one-time password is generated; send it to the person. At first login they must choose their own password before anything else works. An admin-set password for someone else is one-time as well. Roles: reader (view) / admin (change things).

**Start/Stop buttons** (Resources tab; admin; Pilot or Live mode): per-row icon, or select rows then Start.../Stop.... If the schedule disagrees (e.g. VM parked by schedule and you start it) a dialog asks for an override: for N hours, until a date/time in IST, or until cleared, plus a reason. Manual actions go to the front of the queue and send the block/unblock signal like scheduled ones. The separate Override... button is unchanged.

**Add many VMs:** `vm name, provider, project/subscription ID, zone/resource group, schedule` per line (blank or `none` = no schedule; header line skipped; paste or upload CSV). A file with extra columns (such as a start/stop column) must have the schedule name in column 5, otherwise that line is reported as "schedule not found".
