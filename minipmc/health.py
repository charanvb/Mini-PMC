"""Connection checks, shared by `creds-check` (command line) and the dashboard's Connections page.

Read-only: nothing is started, stopped or sent. Each result is {"level": ok | fail | warn, "label", "detail"}.
"""
import socket
import urllib.parse

from . import creds as cr, providers, signals
from .service import Store, UserError

OK, FAIL, WARN = "ok", "fail", "warn"


def can_connect(host: str, port: int = 443, timeout: float = 5.0) -> None:
    """Open (and close) a TCP connection. Proves the network path only - no request is sent."""
    try:
        socket.create_connection((host, port), timeout=timeout).close()
    except OSError as e:
        raise cr.CredentialError(f"cannot open a connection to {host}:{port} ({e}) - firewall or proxy?")


def check_connections(store: Store, vm_name=None, creds=None, connect=can_connect) -> list:
    """Azure identity, Key Vault + GCP key (only if needed), the Logic App, and optionally one VM's power state."""
    results = []

    def step(label, fn, level=FAIL):
        try:
            results.append({"level": OK, "label": label, "detail": str(fn() or "")})
            return True
        except (cr.CredentialError, UserError) as e:
            results.append({"level": level, "label": label, "detail": str(e)})
            return False

    try:
        c = creds or cr.Credentials(cr.load_settings(store.home))
    except cr.CredentialError as e:
        return [{"level": FAIL, "label": "Settings file", "detail": str(e)}]
    vm = store.get_vm(vm_name)[0] if vm_name else None
    gcp_in_use = (vm["provider"] == "gcp") if vm else any(v["provider"] == "gcp" for v in store.list_vms())

    step("Azure managed identity token (ARM)", lambda: c.azure_token(cr.ARM) and "")
    if vm is None or vm["provider"] == "gcp":  # the GCP key is only needed for GCP VMs
        level = FAIL if gcp_in_use else WARN  # not an error yet if no GCP VM has been added
        if step("Key Vault: read GCP key secret", lambda: "(secret found)" if c.gcp_info() else "", level):
            step("GCP service account", lambda: c.gcp_info()["client_email"], level)
            step("Google access token", lambda: c.gcp_token() and "", level)
    app = signals.LogicApp(c)
    if not app.configured:
        results.append({"level": WARN, "label": "Logic App (block/unblock signals)",
                        "detail": "not configured - set logic_app.url_secret_name (or logic_app.url) in "
                                  "config\\settings.json"})
    else:
        found = {}

        def resolve():
            found["url"] = app.url()
            return "found: " + signals.safe_target(found["url"])
        if step("Logic App URL", resolve):
            host = urllib.parse.urlsplit(found["url"]).hostname
            step("Logic App reachable", lambda: connect(host) or f"{host}:443 (connection test only - nothing sent)")
    if vm:
        step("read power state of " + vm["name"],
             lambda: "%s (%s)" % providers.vm_status(c, vm["provider"], vm["scope"], vm["group_or_zone"], vm["name"]))
    return results
