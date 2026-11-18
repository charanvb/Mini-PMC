# Mini PMC – VM guide

Run everything on the Windows Server 2016 VM, in PowerShell. Each part says what to send back. This guide grows with every build step.

> `vms_all.csv` and `config\initial_overrides.csv` contain real VM names. They never go to GitHub: copy them to the VM by hand (RDP clipboard or a shared drive).

## Step 0 - Check the VM (read-only)

```powershell
python --version
python -c "import sys,struct; print(sys.executable, struct.calcsize('P')*8, 'bit')"
python -m pip --version
git --version

# Which places can this VM reach on port 443? (True = reachable)
foreach ($h in 'pypi.org','files.pythonhosted.org','github.com','management.azure.com','login.microsoftonline.com','compute.googleapis.com','oauth2.googleapis.com') {
    $c = $null; $ok = $false
    try { $c = New-Object Net.Sockets.TcpClient; $ok = $c.ConnectAsync($h, 443).Wait(4000) -and $c.Connected } catch { }
    if ($c) { $c.Dispose() }
    '{0,-30} {1}' -f $h, $ok
}

# Azure identity service (read-only)
try { Invoke-RestMethod -Headers @{Metadata='true'} -Uri 'http://169.254.169.254/metadata/instance/compute?api-version=2021-02-01' | Select-Object name, resourceGroupName, location }
catch { "Metadata service not reachable: $($_.Exception.Message)" }
```

**Send back:** the whole output. It decides how we install packages (pip or offline) and how Mini PMC will reach Azure and GCP.

## Step 1 - Get the code onto the VM

**On the laptop, once** (create the GitHub repo as **Private**):

```powershell
cd "<your project folder>"
git init -b main
git add .
git status --short      # vms_all.csv, config\initial_overrides.csv and review\ must NOT be listed
git commit -m "Mini PMC step 1"
git remote add origin https://github.com/<owner>/<repo>.git
git push -u origin main
```

If OneDrive complains about the `.git` folder, work from a folder outside OneDrive, for example `C:\dev\MiniPMC`.

**On the VM:**

```powershell
cd C:\
git clone https://github.com/<owner>/<repo>.git MiniPMC
cd C:\MiniPMC
```

No Git on the VM, or GitHub not reachable? Download the ZIP from GitHub (Code > Download ZIP) on a machine that can, copy it to the VM, then `Expand-Archive .\<repo>-main.zip C:\` and rename the folder to `C:\MiniPMC`.

Every later step starts with `cd C:\MiniPMC; git pull`.

## Step 2 - Install the Python packages

```powershell
cd C:\MiniPMC
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

| Package | Why |
|---|---|
| `tzdata` | time-zone database (Windows has none) |
| `openpyxl` | the Excel review workbook |
| `pytest` | tests only |

All are pure Python, so no compiler is needed.

**Only if Step 0 showed `pypi.org` as False (offline route).** On any Windows machine with internet:

```powershell
python -m pip download -r requirements-dev.txt -d wheels
```

Copy the `wheels` folder to `C:\MiniPMC\wheels`, then on the VM:

```powershell
.\.venv\Scripts\python.exe -m pip install --no-index --find-links wheels -r requirements-dev.txt
```

## Step 3 - Private files (optional - not needed for Step 2 testing)

Only if you want to compare the full inventory numbers. Copy by hand:

| From the laptop | To the VM |
|---|---|
| `vms_all.csv` | `C:\MiniPMC\vms_all.csv` |
| `config\initial_overrides.csv` | `C:\MiniPMC\config\initial_overrides.csv` |

## Step 4 - Run the tests (and optionally build the workbook)

```powershell
cd C:\MiniPMC
.\.venv\Scripts\python.exe -m pytest tests -q
```

Expected without the private files: `... passed, 3 skipped` (no failures). With them: no skips.

Optional workbook (needs the private files):

```powershell
.\.venv\Scripts\python.exe -m minipmc.build_seed --vms vms_all.csv --overrides config\initial_overrides.csv --out review\minipmc_seed.xlsx
```

Expected:

```
VMs: 1891  |  schedule texts: 217 (4 unmanaged)
Schedules: 184  |  managed VMs: 1884  |  unmanaged VMs: 7
To confirm: 6 texts / 19 VMs  |  with an override: 17 (1 until cleared)
Wrote review\minipmc_seed.xlsx
```

## Step 5 - Onboard test VMs by hand (Step 2 of the build)

Run from the cloned folder. `mpmc` is just a shortcut for this session:

```powershell
cd C:\MiniPMC
function mpmc { .\.venv\Scripts\python.exe -m minipmc @args }

mpmc init                                   # creates data\minipmc.db, loads 184 schedules
mpmc schedule list                          # the library
mpmc schedule show "IST Mon-Fri 10:00-18:00"

# add one test VM (use a real name, subscription id and resource group)
mpmc vm add MYTESTVM --provider azure --scope <subscription-id> --group <resource-group> --schedule "ALWAYS_PARKED"
# GCP: mpmc vm add MYGCPVM --provider gcp --scope <project> --group <zone> --schedule ALWAYS_PARKED

mpmc vm list
mpmc desired MYTESTVM --actual running     # what would the scheduler do right now?
mpmc desired MYTESTVM --actual stopped --at 2026-11-23T05:00:00Z   # Monday 10:30 IST -> START

mpmc override set MYTESTVM --days 1 --reason "testing"
mpmc desired MYTESTVM --actual running     # -> SKIP / OVERRIDE_ACTIVE
mpmc override clear MYTESTVM

mpmc validate
mpmc vm show MYTESTVM                      # recent events
mpmc vm remove MYTESTVM
```

Nothing here touches Azure or GCP: it only edits the local database. Every change is written to `logs\app.log` and `logs\audit\<date>.jsonl`.

To load the whole inventory later: `mpmc import --vms vms_all.csv --overrides config\initial_overrides.csv` (dry run), add `--apply` to write.

**Send back:** the output of `mpmc desired` (both), `mpmc validate`, and the last lines of `logs\audit\<today>.jsonl`.

## If something fails

| Symptom | Fix |
|---|---|
| `python` not found | try `py -3`, or use the full path to `python.exe` |
| pip cannot connect / SSL or proxy error | send me the message; use the offline route in Step 2 |
| `validate` shows ERROR "time zone cannot be loaded" | `tzdata` is missing: `.\.venv\Scripts\python.exe -m pip install tzdata` |
| `Unable to copy venvlauncher.exe` | `python -m venv` was run twice while the venv was in use; harmless, do not repeat |
| `Activate.ps1` blocked | not needed; this guide always calls `.\.venv\Scripts\python.exe` directly |

## Logic App URL and Omnibus signals

The Logic App trigger's full **HTTP POST URL** contains a signature (`sig=`). Keep it out of Git and logs.
Recommended: create a Key Vault secret (for example `logicapp-omnibus-url`) containing the complete URL,
then set `logic_app.url_secret_name` in the git-ignored `config\settings.json`:

```json
{
  "logic_app": {
    "url_secret_name": "logicapp-omnibus-url",
    "timeout_seconds": 30,
    "retries": 3
  }
}
```

The attached `CloudBAU-Automation` user-assigned managed identity needs **Key Vault Secrets User** on that vault.
For a quick, non-production test, `logic_app.url` can hold the URL directly in `config\settings.json` instead;
the secret-name setting is preferred.

Mini PMC sends `{"vms":[{"name":"...","project":"...","zone":"..."}],"action":"block"}` before a stop and
`"unblock"` before a start. For Azure, `project` is the subscription ID and `zone` is the resource group; for GCP
they are the project ID and zone. It does not poll the Logic App or wait after acceptance: on a 2xx response,
Mini PMC immediately calls the corresponding VM start/stop API.
Use **Settings > Connections** to check configuration and send a manually confirmed test signal. Do not send
test signals to production Omnibus unless you intend to change the VM's alert state.

The v3 database migration renames old hold records and events to overrides. Existing append-only JSONL audit
files are not rewritten, so older files may still contain `HOLD_*` event names.
