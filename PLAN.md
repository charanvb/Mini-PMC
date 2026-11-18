# Mini PMC – Plan (v4)

Small, reliable start/stop scheduler for the 1,891 VMs in `vms_all.csv` (1,123 Azure / 768 GCP). One server (Windows Server 2016), no external database, everything logged. Built in small steps; each step is tested on the laptop and then on the VM ([docs/VM-GUIDE.md](docs/VM-GUIDE.md)).

**Build status:** the schedule engine, SQLite store, CLI, local login, cloud credentials, REST APIs, portal UI, bulk status collector, queued executor (waves of 100 VMs/minute, fire-and-forget block/unblock signals, host guard), email reports/alerts via Logic App, Excel export and the watchdog are implemented (139 tests pass). Remaining: the pilot and go-live on the real estate, following `readonly` -> `dryrun` -> `pilot` -> `live` (`python -m minipmc mode <name>` or the dashboard "Change mode").

## 1. Decisions
| Topic | Decision |
|---|---|
| Name | **Mini PMC** |
| Schedule | One **7x24 grid of 15-minute cells** per named schedule, plus an IANA time zone. Each cell is Running or Stopped; outside the up windows a VM is Stopped. The 213 readable schedule texts became 184 schedules with readable names, e.g. `IST Mon-Fri 10:00-22:00` |
| Scheduler output | START, STOP or SKIP with a reason. VMs already in the right state are not logged |
| Reconcile | every **15 min** (aligned to the grid); status collection every **5 min** |
| Override | While an override is active Mini PMC takes **no action**: no start, no stop, and no block/unblock signal. It has an optional end time and reason; no end = until a person clears it. When it ends the VM returns to its schedule. An override never starts a stopped VM. |
| Unmanaged | A text that cannot be turned into times (4 texts / 7 VMs, e.g. "IL QA backup 3") leaves the VM unmanaged: never touched, red on the Unmanaged tab now, flagged in the dashboard later |
| Maintenance texts | "Always Parked (excl. 10hr maintenance)" = parked, up **Sunday 00:00-10:00** in the text's zone; **Thursday** only for the "Thursday/GMT" variant; with no zone it is unmanaged |
| `Action` column | ignored |
| Time zones | logic in UTC; each schedule has a zone. BST = Europe/London (Asia/Dhaka if the text says Dhaka); GMT = fixed UTC; an explicit `GMT+n` / `UTC+n` wins over a name. Full list: Timezones tab of the seed workbook |
| Storage | one **SQLite** file `data/minipmc.db`; Excel / CSV for bulk import and export (Step 2) |
| Add / remove VMs | dashboard form or CSV import; a removed VM is disabled, never erased from the audit trail |
| Login | local username + password (hashed); two roles: admin (everything) / reader (look only). No Entra for now |
| Logic App | POST `{"vms":[{"name","project","zone"}],"action":"block"}` or `"unblock"`. Azure: project = subscription ID, zone = resource group. **No polling or settle delay: HTTP 2xx = accepted, then call the VM start/stop API immediately.** UNBLOCK before start, BLOCK before stop. Keep the signed HTTP POST URL in Key Vault and configure `logic_app.url_secret_name` in the git-ignored `config\settings.json`. |
| Rollout | `readonly` -> `dryrun` -> `pilot` (allow-list) -> `live` |

### Confirmed on the seed data
- 17 overrides from `config/initial_overrides.csv` (git-ignored): 16 until **31 Dec 2026 23:59:59 UTC** (= 05:30 IST on 1 Jan), 1 permanent. The permanent one is the Mini PMC host, which has an ALWAYS_UP schedule.
- All 16 dated overrides sit on parked schedules, so any of them still running are **stopped after the override expires**. The dashboard shows overrides ending soon.
- Readings confirmed: Israel "(Sun-Fri)" = 6 days Sun-Fri; "5 days (Mon-Sat) 10 AM to 4:30 AM IST" = starts Mon-Fri, last stop Sat 04:30; EST / PST / AEST follow daylight saving; "Egypt(UTC+2)" = fixed UTC+2.

## 2. Architecture (one process, one Windows service via WinSW)
```
Browser -> [ Dashboard + API ]   --\
           [ Collector  (5 min) ] ---> SQLite  <-- logs/ (app, error, audit.jsonl)
           [ Reconciler (15 min)] --/
           [ Executor (workers) ] ---> Logic App -> Azure / GCP APIs
```
- The loops are threads of one process; if the dashboard breaks, scheduling keeps running.
- Jobs are stored in SQLite; after a restart, in-flight jobs are checked against real cloud state, never blindly retried.
- Safety: no action on UNKNOWN, stale (> 15 min) or transitional status; one operation per VM; global pause; concurrency cap with 429 back-off; Mini PMC never stops its own host VM. Azure stop = deallocate; GCP stop = `instances.stop`.
- Dashboard server: the **Python standard library (`http.server`) is recommended** - nothing to install, and testable on the laptop. FastAPI is the alternative. Decided at Step 3.

## 3. Logging
`logs/app.log`, `logs/error.log`, `logs/collector.log`, `logs/audit/YYYY-MM-DD.jsonl`. Every START / STOP and every notable skip is audited: UTC + local time, VM, schedule, desired vs actual state, reason, override, Logic App result, operation ID, outcome, user. Logins, overrides and config changes are audited too. Viewable in the dashboard. Old append-only JSONL files may retain historical `HOLD_*` event names.

## 4. Dependencies on the VM
Rule: **pure-Python packages only.** The VM runs Python 3.15.2 and compiled wheels (pydantic-core, cryptography, ...) may not exist for it yet; the laptop cannot download packages.

| When | What | How |
|---|---|---|
| Now | Python 3.15.2 (already on the VM); Git for Windows, or a ZIP of the repo | - |
| Now | pip packages `tzdata`, `openpyxl`, `pytest` (tests only) | `pip install -r requirements-dev.txt`, or offline from a `wheels` folder |
| Step 3 | nothing, if the standard-library web server is chosen | - |
| Step 4 | Azure: nothing (managed identity + REST). GCP: depends on how the VM signs in | decided at Step 4 |
| Step 6 | WinSW (one .exe) to run as a Windows service | copy the exe |

Outbound HTTPS allow-list: `pypi.org` and `files.pythonhosted.org` (install only), `github.com` (pull), `management.azure.com`, `login.microsoftonline.com`, `compute.googleapis.com`, `oauth2.googleapis.com`, and the Logic App host. Azure access uses the **CloudBAU-Automation user-assigned managed identity** on the VM.

`vms_all.csv` and `config/initial_overrides.csv` hold real VM names. They are git-ignored and go to the VM by hand, never through GitHub.

## 5. Repo layout
```
minipmc/   grid.py  reconcile.py  importer.py  tz.py  build_seed.py        (Step 1)
tests/     test_core.py  test_importer.py
config/    initial_overrides.example.csv     (real initial_overrides.csv is git-ignored)
docs/      VM-GUIDE.md
PLAN.md  requirements.txt  requirements-dev.txt  .gitignore
git-ignored: vms_all.csv  config/initial_overrides.csv  review/  data/  logs/  .venv/  wheels/
```
Later steps add `minipmc/` modules for storage, CLI, web, providers and the executor, and `deploy/` for the service files.

## 6. Build steps (stop for approval after each)
| Step | Scope | Status |
|---|---|---|
| 1 | Grid engine, reconcile decision, schedule-text importer, override file, seed workbook `review/minipmc_seed.xlsx`, tests | **Done** |
| 1b | Run the engine tests on the VM | **Complete** |
| 2 | SQLite storage, logging, CLI, schedules and override management | **Complete** |
| 3 | Dashboard + login: VMs, schedule grid editor, overrides, audit and unmanaged resources | **Implemented; browser QA in progress** |
| 4 | Azure / GCP providers + collector (read-only status) | **Done** (bulk list per subscription/project) |
| 5 | Executor + Logic App client in dry-run, crash recovery, host guard | **Done** (SQLite ops queue, waves, stuck detection) |
| 6 | Windows scheduled service, production hardening, pilot runbook; then pilot and live | **Task + watchdog + /healthz done; pilot pending** |

## 7. Still open (asked when we reach the step)
- One-click "Start now" / "Stop now" in the dashboard?
- TLS termination and the appropriate network exposure before production use.
