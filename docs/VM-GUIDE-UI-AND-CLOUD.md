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
python -m minipmc user add ops --role operator     # operator = holds/pause; viewer = read only
python -m minipmc serve --port 8080                # test run, Ctrl+C to stop
```
Open http://localhost:8080. Roles: viewer (read), operator (+ holds, pause), admin (+ VMs, schedules, users).
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
1. Portal > Managed Identities > Create (e.g. `id-minipmc`). Copy its **Client ID**.
2. VM `bnlwefspap20560` > Identity > User assigned > Add `id-minipmc`.
3. Key Vault > Access control (IAM) > give `id-minipmc` the role **Key Vault Secrets User**.
   If the vault has a firewall, allow this VM's network.
4. Subscriptions/resource groups holding the VMs > IAM > give `id-minipmc` **Reader** now
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
   notepad config\settings.json     # uami_client_id, key_vault_url, key_secret_name
   ```
   `settings.json` is git-ignored.

## 6. Check
```
python -m minipmc creds-check
python -m minipmc creds-check --vm <azure-vm-name>
python -m minipmc creds-check --vm <gcp-vm-name>
```
This only reads; it never starts or stops anything. Paste the output back.
