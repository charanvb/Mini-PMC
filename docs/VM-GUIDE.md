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

## Step 3 - Put the private files on the VM

Copy by hand:

| From the laptop | To the VM |
|---|---|
| `vms_all.csv` | `C:\MiniPMC\vms_all.csv` |
| `config\initial_overrides.csv` | `C:\MiniPMC\config\initial_overrides.csv` |

## Step 4 - Run the tests and build the workbook

```powershell
cd C:\MiniPMC
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe -m minipmc.build_seed --vms vms_all.csv --overrides config\initial_overrides.csv --out review\minipmc_seed.xlsx
```

Expected:

```
79 passed in ...
VMs: 1891  |  schedule texts: 217 (4 unmanaged)
Schedules: 184  |  managed VMs: 1884  |  unmanaged VMs: 7
To confirm: 6 texts / 19 VMs  |  on hold: 17 (1 until cleared)
Wrote review\minipmc_seed.xlsx
```

The laptop shows `78 passed, 1 skipped`. The extra test checks the London clock change and only runs where `tzdata` is installed, so it runs for the first time here.

**Send back:** the output of both commands, or the error text.

## If something fails

| Symptom | Fix |
|---|---|
| `python` not found | try `py -3`, or use the full path to `python.exe` |
| pip cannot connect / SSL or proxy error | send me the message; use the offline route in Step 2 |
| `1 skipped` in the test result | `tzdata` is missing: `.\.venv\Scripts\python.exe -m pip install tzdata` |
| `Activate.ps1` blocked | not needed; this guide always calls `.\.venv\Scripts\python.exe` directly |
