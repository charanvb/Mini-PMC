"""Power-state collector: reads each VM's state from the cloud and keeps the last answer in SQLite.

Used by the background loop (every 5 minutes, all VMs) and by the dashboard's "Refresh status" buttons (the VMs
the user picked). Both call `refresh`. It only reads; nothing is started or stopped here.
"""
import logging
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from . import creds as cr, db, providers, reconcile as rc
from .logs import utcnow_iso
from .service import UserError, parse_utc

log = logging.getLogger("minipmc.status")

WORKERS = 8
BULK_MIN = 5  # below this many VMs in one subscription, single reads are cheaper than a list
NAME_RETRY = timedelta(hours=6)  # how long before asking again for a subscription name that could not be read


# ---------------------------------------------------------------- stored status

def all_status(store) -> dict:
    """{vm name (lower case): row} for every VM that has been read at least once."""
    with db.connect(store.home) as con:
        return {r["vm_name"].lower(): dict(r) for r in con.execute("SELECT * FROM vm_status")}


def record(store, name: str, state: str, raw: str = "", error: str = "") -> None:
    """Save one reading. A failed reading keeps the old `raw` out of it and counts up `fails`."""
    now = utcnow_iso()
    with db.connect(store.home) as con:
        old = con.execute("SELECT fails FROM vm_status WHERE vm_name=?", (name,)).fetchone()
        fails = (old["fails"] + 1) if (old and error) else (1 if error else 0)
        con.execute("INSERT OR REPLACE INTO vm_status (vm_name, state, raw, checked_at, error, fails) VALUES (?,?,?,?,?,?)",
                    (name, state, raw, now, error[:300], fails))


def beat(store, key: str) -> None:
    """Heartbeat: the time a background loop last ran. Plain settings row, not audited."""
    with db.connect(store.home) as con:
        con.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", ("hb:" + key, utcnow_iso()))


def beat_age(store, key: str, now=None):
    """Seconds since the last heartbeat, or None if it has never beaten."""
    with db.connect(store.home) as con:
        r = con.execute("SELECT value FROM settings WHERE key=?", ("hb:" + key,)).fetchone()
    return None if not r else ((now or datetime.now(timezone.utc)) - parse_utc(r[0])).total_seconds()


# ---------------------------------------------------------------- subscription names

def scope_names(store) -> dict:
    """{subscription id: display name} for the names already looked up."""
    with db.connect(store.home) as con:
        return {r["scope"]: r["name"] for r in con.execute("SELECT scope, name FROM sub_names") if r["name"]}


def resolve_names(store, creds, scopes, now=None) -> None:
    """Look up the display name of each Azure subscription id not known yet. Needs Reader on the subscription."""
    now = now or datetime.now(timezone.utc)
    with db.connect(store.home) as con:
        known = {r["scope"]: (r["name"], parse_utc(r["fetched_at"])) for r in con.execute("SELECT * FROM sub_names")}
    for scope in sorted(set(scopes)):
        name, at = known.get(scope, ("", None))
        if name or (at and now - at < NAME_RETRY):
            continue
        try:
            token = creds.azure_token(cr.ARM)
            status, body = creds.http("GET", f"{cr.ARM}subscriptions/{urllib.parse.quote(scope)}?api-version=2022-12-01",
                                      {"Authorization": "Bearer " + token})
            name = body.get("displayName", "") if status == 200 else ""
            if not name:
                log.warning("subscription name for %s not available (HTTP %s)", scope, status)
        except Exception as e:
            log.warning("subscription name for %s not available (%s)", scope, type(e).__name__)
            name = ""
        with db.connect(store.home) as con:
            con.execute("INSERT OR REPLACE INTO sub_names VALUES (?,?,?)", (scope, name, utcnow_iso()))


# ---------------------------------------------------------------- reading the cloud

def read_one(creds, vm):
    """(state, raw, error) for one VM row. Never raises: a failed reading becomes UNKNOWN plus the reason."""
    try:
        state, raw = providers.vm_status(creds, vm["provider"], vm["scope"], vm["group_or_zone"], vm["name"])
        return state, raw, ""
    except cr.CredentialError as e:
        return rc.UNKNOWN, "", str(e)
    except Exception as e:  # e.g. a timeout while reading; keep only the type so no URL or token can leak
        return rc.UNKNOWN, "", f"{type(e).__name__} while reading the status"


def record_many(store, rows) -> None:
    """Save many readings in one transaction. rows: (name, state, raw, error)."""
    now = utcnow_iso()
    with db.connect(store.home) as con:
        fails = {r["vm_name"]: r["fails"] for r in con.execute("SELECT vm_name, fails FROM vm_status")}
        for name, state, raw, error in rows:
            n = (fails.get(name, 0) + 1) if error else 0
            con.execute("INSERT OR REPLACE INTO vm_status (vm_name, state, raw, checked_at, error, fails) "
                        "VALUES (?,?,?,?,?,?)", (name, state, raw, now, error[:300], n))


def _key(v):
    return ((v["group_or_zone"] or "").lower(), v["name"].lower())


def refresh(store, creds, names=None, workers: int = WORKERS) -> dict:
    """Read the live state of `names` (all VMs if None) and store it. Returns {"done", "errors"}.

    Several VMs in one subscription/project are read with one list call; the rest (and any VM the list did not
    answer for) one by one. One failed VM does not stop the others."""
    vms = store.list_vms()
    errors = []
    if names is not None:
        by_name = {v["name"].lower(): v for v in vms}
        wanted = list(dict.fromkeys(n.lower() for n in names))
        missing = [n for n in wanted if n not in by_name]
        if len(wanted) == 1 and missing:
            raise UserError(f"VM not found: '{names[0]}'")
        errors = [f"{n}: VM not found" for n in missing]
        vms = [by_name[n] for n in wanted if n in by_name]

    resolve_names(store, creds, [v["scope"] for v in vms if v["provider"] == "azure"])

    # Fetch each cloud's token once up front, so a broken identity fails fast instead of once per VM.
    broken = {}
    for provider, warm in (("azure", lambda: creds.azure_token(cr.ARM)), ("gcp", lambda: creds.gcp_token())):
        if any(v["provider"] == provider for v in vms):
            try:
                warm()
            except cr.CredentialError as e:
                broken[provider] = str(e)
            except Exception as e:
                broken[provider] = f"{type(e).__name__} while signing in"

    results = {}  # vm name -> (state, raw, error)
    groups = {}
    for v in vms:
        if v["provider"] in broken:
            results[v["name"]] = (rc.UNKNOWN, "", broken[v["provider"]])
        else:
            groups.setdefault((v["provider"], v["scope"]), []).append(v)

    def bulk(item):
        (provider, scope), members = item
        if len(members) < BULK_MIN:
            return members, {}
        try:
            return members, providers.list_scope(creds, provider, scope)
        except Exception as e:  # fall back to reading one by one
            log.warning("list of %s %s failed (%s); reading VMs one by one", provider, scope, type(e).__name__)
            return members, {}

    rest = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for members, listing in pool.map(bulk, groups.items()):
            for v in members:
                hit = listing.get(_key(v))
                if hit:
                    results[v["name"]] = (hit[0], hit[1], "")
                else:
                    rest.append(v)
        for v, res in zip(rest, pool.map(lambda v: read_one(creds, v), rest)):
            results[v["name"]] = res

    record_many(store, [(n, s, r, e) for n, (s, r, e) in results.items()])
    errors += [f"{n}: {e}" for n, (_, _, e) in results.items() if e]
    return {"done": sum(1 for _, _, e in results.values() if not e), "errors": errors}
