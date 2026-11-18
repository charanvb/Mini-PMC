"""Command line: python -m minipmc <command>. Output is ASCII-only so any Windows console shows it correctly."""
import argparse
import getpass
import json
import os
import sys
from datetime import datetime, timedelta, timezone

from . import db, grid as g, health, importer, reconcile as rc, signals, users
from .logs import setup_logging
from .service import Store, UserError, iso, parse_until, parse_utc
from .users import ROLES


def out(*parts) -> None:
    text = " ".join(str(p) for p in parts)
    print(text.encode("ascii", "replace").decode("ascii"))


def table(rows, headers) -> None:
    rows = [[("" if c is None else str(c)) for c in r] for r in rows]
    widths = [max([len(h)] + [len(r[i]) for r in rows]) for i, h in enumerate(headers)]
    out("  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip())
    out("  ".join("-" * w for w in widths))
    for r in rows:
        out("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="minipmc", description="Mini PMC - VM schedule manager")
    p.add_argument("--home", default=os.environ.get("MINIPMC_HOME", os.getcwd()),
                   help="folder holding data\\ and logs\\ (default: current folder)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create the database and load the schedule library")

    sp = sub.add_parser("schedule", help="schedules").add_subparsers(dest="sub", required=True)
    sp.add_parser("list")
    x = sp.add_parser("show"); x.add_argument("name")
    x = sp.add_parser("add", help="create a schedule from schedule text"); x.add_argument("text")
    x.add_argument("--name")
    x = sp.add_parser("remove"); x.add_argument("name")
    x = sp.add_parser("test", help="show how a schedule maps UTC / London time to its own time zone")
    x.add_argument("name"); x.add_argument("--at", help="UTC time, e.g. 2026-03-29T00:30:00Z (default: now)")

    vp = sub.add_parser("vm", help="VMs").add_subparsers(dest="sub", required=True)
    x = vp.add_parser("list"); x.add_argument("--find", default=""); x.add_argument("--unmanaged", action="store_true")
    x = vp.add_parser("show"); x.add_argument("name")
    x = vp.add_parser("add")
    x.add_argument("name")
    x.add_argument("--provider", required=True, choices=["azure", "gcp"])
    x.add_argument("--scope", required=True, help="Azure subscription id / GCP project")
    x.add_argument("--group", required=True, help="Azure resource group / GCP zone")
    x.add_argument("--schedule"); x.add_argument("--notes", default="")
    x = vp.add_parser("remove"); x.add_argument("name")
    x = vp.add_parser("enable"); x.add_argument("name")
    x = vp.add_parser("disable"); x.add_argument("name")
    x = vp.add_parser("set-schedule"); x.add_argument("name"); x.add_argument("schedule", help="name, or '-' for none")

    op = sub.add_parser("override", help="overrides: while a VM has one, the scheduler does nothing to it"
                        ).add_subparsers(dest="sub", required=True)
    x = op.add_parser("set"); x.add_argument("name")
    g1 = x.add_mutually_exclusive_group(required=True)
    g1.add_argument("--until", help="2026-12-31 (end of day UTC) or 2026-12-31T18:00:00Z")
    g1.add_argument("--days", type=float, help="override for this many days from now")
    g1.add_argument("--forever", action="store_true", help="until someone clears it")
    x.add_argument("--reason", required=True)
    x = op.add_parser("clear"); x.add_argument("name")
    op.add_parser("list")

    x = sub.add_parser("desired", help="what would the scheduler do for this VM?")
    x.add_argument("name"); x.add_argument("--at", help="UTC time, e.g. 2026-11-22T00:30:00Z")
    x.add_argument("--actual", default="running", choices=["running", "stopped", "deallocated"])

    x = sub.add_parser("import", help="add VMs from an inventory CSV (dry run unless --apply)")
    x.add_argument("--vms", required=True); x.add_argument("--overrides"); x.add_argument("--apply", action="store_true")

    x = sub.add_parser("library-export", help="write config\\schedule_library.csv from an inventory CSV")
    x.add_argument("--vms", required=True); x.add_argument("--out")

    sub.add_parser("validate", help="check the setup for problems")
    x = sub.add_parser("serve", help="run the dashboard and the scheduler (the background loops that start/stop VMs)")
    x.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to let the team reach it")
    x.add_argument("--port", type=int, default=8080)
    x.add_argument("--cert", default="", help="PEM certificate (chain) file: serve HTTPS instead of HTTP")
    x.add_argument("--key", default="", help="PEM private key file for --cert")
    x.add_argument("--no-scheduler", action="store_true", help="dashboard only (no status reads, no start/stop)")
    x = sub.add_parser("make-cert", help="create a self-signed HTTPS certificate (needs: pip install cryptography)")
    x.add_argument("--names", required=True, help="host names / IPs users will type, comma-separated")
    x.add_argument("--days", type=int, default=825)
    x.add_argument("--out", help="folder for minipmc.crt and minipmc.key (default: <home>\\certs)")
    x = sub.add_parser("pfx-to-pem", help="convert a .pfx/.p12 certificate to the PEM files serve needs (needs: pip install cryptography)")
    x.add_argument("pfx"); x.add_argument("--password", default="")
    x.add_argument("--out", help="folder for minipmc.crt and minipmc.key (default: <home>\\certs)")
    x = sub.add_parser("mode", help="show or set what the scheduler may do: readonly | dryrun | pilot | live")
    x.add_argument("name", nargs="?", choices=["readonly", "dryrun", "pilot", "live"])
    x.add_argument("--vms", help="pilot only: comma-separated VM names")
    x = sub.add_parser("cycle", help="run one scheduler cycle now (reads statuses, plans, sends one wave); uses the current mode")

    up = sub.add_parser("user", help="dashboard users").add_subparsers(dest="sub", required=True)
    x = up.add_parser("add"); x.add_argument("username"); x.add_argument("--role", default="reader", choices=list(ROLES))
    x.add_argument("--password", help="omit to be prompted (recommended)")
    x = up.add_parser("passwd"); x.add_argument("username"); x.add_argument("--password")
    x = up.add_parser("role"); x.add_argument("username"); x.add_argument("role", choices=list(ROLES))
    for n in ("enable", "disable", "remove"):
        x = up.add_parser(n); x.add_argument("username")
    up.add_parser("list")

    x = sub.add_parser("creds-check", help="test Azure identity, Key Vault, GCP key and the Logic App; optionally read "
                                           "one VM's state")
    x.add_argument("--vm", help="name of a VM already added; its power state is read (read-only)")
    x = sub.add_parser("signal-test", help="show (and with --send, really send) a block/unblock signal for one VM")
    x.add_argument("--vm", required=True, help="name of a VM already added")
    x.add_argument("--action", choices=list(signals.ACTIONS), default="unblock")
    x.add_argument("--send", action="store_true", help="really send it to the Logic App (default: show only)")
    sub.add_parser("pause", help="stop the scheduler from acting on anything")
    sub.add_parser("resume")
    x = sub.add_parser("export", help="write the VM list to CSV"); x.add_argument("--out", required=True)
    return p


def creds_check(store: Store, vm_name=None) -> int:
    results = health.check_connections(store, vm_name)
    for r in results:
        detail = r["detail"]
        if r["level"] == "ok":
            out("OK    ", r["label"], detail)
        else:
            out(r["level"].upper().ljust(6), r["label"], "-", detail)
    return 1 if any(r["level"] == "fail" for r in results) else 0


def signal_test(store: Store, vm_name: str, action: str, send: bool) -> int:
    """Show the exact message for one VM; only `--send` really calls the Logic App (and writes the audit event)."""
    from . import creds as cr
    vm = store.get_vm(vm_name)[0]
    app = signals.LogicApp(cr.Credentials(cr.load_settings(store.home)))
    out("message :", json.dumps(signals.payload(action, [vm])))
    out("sent to :", signals.safe_target(app.url()), "(the secret part of the URL is never shown)")
    if not send:
        out("\nDRY RUN - nothing was sent. Add --send to really send this signal.")
        return 0
    r = signals.send_signal(store, action, [vm], app=app, reason="manual test (signal-test)")
    out(("ACCEPTED by the Logic App" if r["ok"] else "FAILED") + ":", r["detail"], "(attempts: %d)" % r["attempts"])
    return 0 if r["ok"] else 1


def run(args, store: Store) -> int:
    c, s = args.cmd, getattr(args, "sub", None)

    if c == "init":
        r = store.init()
        out("database created" if r["created"] else "database already exists",
            "- schedules added: %d" % r["schedules_added"])
        out("path:", db.db_path(store.home))
        return 0

    if c == "schedule":
        if s == "list":
            table([(r["name"], r["tz"], r["vm_count"], r["description"]) for r in store.list_schedules()],
                  ["NAME", "TIME ZONE", "VMS", "DESCRIPTION"])
        elif s == "show":
            r = store.get_schedule(args.name)
            out(r["name"], "|", r["tz"], "|", r["description"])
            out("Running windows (local time of the schedule):")
            for a, n in g.runs(g.from_string(r["grid"])):
                out("  ", g.run_label(a, n))
        elif s == "add":
            out("schedule ready:", store.add_schedule_from_text(args.text, args.name))
        elif s == "test":
            r = store.time_check(args.name, parse_utc(args.at) if args.at else None)
            out("%s  (time zone %s)" % (r["schedule"], r["tz"]))
            n = r["now"]
            out("At UTC %s | London %s | schedule %s  ->  should be %s" %
                (n["utc"], n["london"], n["schedule"], "RUNNING" if r["now_state"] == "R" else "STOPPED"))
            out("Next switches:")
            for w in r["switches"]:
                out("  -> %-7s UTC %s | London %s | schedule %s" %
                    ("RUNNING" if w["to"] == "R" else "STOPPED", w["utc"], w["london"], w["schedule"]))
        elif s == "remove":
            store.remove_schedule(args.name)
            out("removed", args.name)
        return 0

    if c == "vm":
        if s == "list":
            rows = store.list_vms(args.find, args.unmanaged)
            table([(r["name"], r["provider"], r["schedule"] or "(unmanaged)", "yes" if r["enabled"] else "NO",
                    ("until " + r["override_until"]) if r["overridden"] and r["override_until"]
                    else ("OVERRIDE" if r["overridden"] else ""))
                   for r in rows], ["NAME", "PROVIDER", "SCHEDULE", "ENABLED", "OVERRIDE"])
            out("\n%d VM(s)" % len(rows))
        elif s == "show":
            vm, override, events = store.get_vm(args.name)
            for k in ("name", "provider", "scope", "group_or_zone", "schedule", "enabled", "notes", "created_at"):
                out("%-14s %s" % (k, vm[k]))
            out("%-14s %s" % ("override", ("until " + (override["expires_at"] or "cleared by a person") + " - " +
                                           override["reason"]) if override else "none"))
            out("\nrecent events:")
            for e in events:
                out(" ", e["ts"], e["actor"], e["action"], e["detail"])
        elif s == "add":
            store.add_vm(args.name, args.provider, args.scope, args.group, args.schedule, args.notes)
            out("added", args.name)
        elif s == "remove":
            store.remove_vm(args.name); out("removed", args.name)
        elif s in ("enable", "disable"):
            store.set_enabled(args.name, s == "enable"); out(s + "d", args.name)
        elif s == "set-schedule":
            store.set_schedule(args.name, None if args.schedule == "-" else args.schedule)
            out("updated", args.name)
        return 0

    if c == "override":
        if s == "set":
            until = None if args.forever else (parse_until(args.until) if args.until else
                                               datetime.now(timezone.utc) + timedelta(days=args.days))
            store.set_override(args.name, until, args.reason)
            out("override set on", args.name, "until", iso(until) or "cleared by a person")
        elif s == "clear":
            store.clear_override(args.name); out("override cleared on", args.name)
        elif s == "list":
            table([(o["vm_name"], o["expires_at"] or "until cleared", o["set_by"], o["reason"])
                   for o in store.list_overrides()], ["VM", "ENDS (UTC)", "SET BY", "REASON"])
        return 0

    if c == "desired":
        at = parse_utc(args.at) if args.at else None
        r = store.explain(args.name, at, args.actual)
        for k in ("vm", "schedule", "at_utc", "local_time", "assumed_state", "schedule_says", "action", "reason"):
            if k in r:
                out("%-14s %s" % (k, r[k]))
        return 0

    if c == "import":
        cat = importer.build_catalog(args.vms)
        if args.overrides:
            try:
                rows = importer.load_overrides(args.overrides)
            except ValueError as e:
                raise UserError(f"{args.overrides}: {e}")
            for e in importer.apply_overrides(cat, rows):
                out("WARN", e)
        r = store.import_catalog(cat, args.apply)
        out(("IMPORTED" if args.apply else "DRY RUN (nothing written; add --apply)") + ":", r)
        return 0

    if c == "library-export":
        cat = importer.build_catalog(args.vms)
        from .service import LIBRARY_PATH
        out("wrote", store.export_library(cat, args.out or LIBRARY_PATH), "schedules")
        return 0

    if c == "validate":
        items = store.validate()
        for lvl, msg in items:
            out("%-5s %s" % (lvl, msg))
        n_err = sum(1 for lvl, _ in items if lvl == "ERROR")
        out("\n%d error(s), %d warning(s)" % (n_err, sum(1 for lvl, _ in items if lvl == "WARN")))
        return 1 if n_err else 0

    if c in ("pause", "resume"):
        store.set_setting("paused", "1" if c == "pause" else "0"); out("scheduler", c + "d")
        return 0

    if c == "serve":
        from . import web
        out("dashboard on %s://%s:%d  (Ctrl+C to stop)" % ("https" if args.cert else "http", args.host, args.port))
        web.serve(store.home, args.host, args.port, run_scheduler=not args.no_scheduler, cert=args.cert, key=args.key)
        return 0

    if c in ("make-cert", "pfx-to-pem"):
        import os
        from . import certs
        folder = args.out or os.path.join(store.home, "certs")
        if c == "make-cert":
            crt, key = certs.make_self_signed([n.strip() for n in args.names.split(",") if n.strip()], folder, args.days)
        else:
            crt, key = certs.pfx_to_pem(args.pfx, args.password, folder)
        out("certificate:", crt); out("private key:", key)
        out("start with: serve --port 443 --cert \"%s\" --key \"%s\"" % (crt, key))
        return 0

    if c == "mode":
        if args.name:
            if args.name == "pilot":
                names = [n.strip() for n in (args.vms or "").split(",") if n.strip()]
                if not names:
                    raise UserError("pilot needs --vms vm1,vm2")
                for n in names:
                    store.get_vm(n)
                store.set_setting("pilot_vms", ",".join(names))
            if args.name == "live":
                if input("Type LIVE to let the scheduler start and stop every managed VM: ").strip() != "LIVE":
                    raise UserError("not confirmed; mode unchanged")
            store.set_setting("mode", args.name)
            store.set_setting_quiet("last_slot", "")
        out("mode:", store.get_setting("mode", "readonly"),
            ("  pilot VMs: " + store.get_setting("pilot_vms", "(none)")) if store.get_setting("mode") == "pilot" else "")
        return 0

    if c == "cycle":
        from . import executor
        eng = executor.Engine(store)
        out("cycle:", eng.tick())
        out("sent to the cloud:", eng.pump(force=True))
        return 0

    if c == "user":
        if s == "list":
            table([(u["username"], u["role"], "DISABLED" if u["disabled"] else "active") for u in users.list_users(store)],
                  ["USER", "ROLE", "STATUS"])
        elif s in ("add", "passwd"):
            pw = args.password
            if pw is None:
                pw = getpass.getpass("Password (8+ characters): ")
                if pw != getpass.getpass("Repeat password: "):
                    raise UserError("passwords do not match")
            if s == "add":
                users.add_user(store, args.username, pw, args.role)
            else:
                users.set_password(store, args.username, pw)
            out("done:", s, args.username)
        elif s == "role":
            users.set_role(store, args.username, args.role); out("done")
        elif s in ("enable", "disable"):
            users.set_disabled(store, args.username, s == "disable"); out("done")
        elif s == "remove":
            users.remove_user(store, args.username); out("removed", args.username)
        return 0

    if c == "creds-check":
        return creds_check(store, args.vm)

    if c == "signal-test":
        return signal_test(store, args.vm, args.action, args.send)

    if c == "export":
        out("wrote", store.export_vms_csv(args.out), "VMs to", args.out)
        return 0
    return 2


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    home = os.path.abspath(args.home)
    try:
        setup_logging(home)
        if args.cmd != "init" and os.path.exists(db.db_path(home)):
            db.init(home)  # applies any pending schema upgrade
        return run(args, Store(home, actor=getpass.getuser()))
    except (UserError, FileNotFoundError) as e:
        out("ERROR:", e)
        return 1
    except Exception as e:
        from .creds import CredentialError
        if isinstance(e, CredentialError):
            out("ERROR:", e)
            return 1
        raise
