"""Read-only VM status lookups (Azure Resource Manager and Google Compute). No start/stop here."""
import urllib.parse

from . import reconcile as rc
from .creds import ARM, CredentialError, Credentials

_AZURE = {"running": rc.RUNNING, "stopped": rc.STOPPED, "deallocated": rc.DEALLOCATED, "starting": rc.STARTING,
          "stopping": rc.STOPPING, "deallocating": rc.STOPPING}
_GCP = {"RUNNING": rc.RUNNING, "TERMINATED": rc.STOPPED, "STOPPING": rc.STOPPING, "PROVISIONING": rc.STARTING,
        "STAGING": rc.STARTING, "REPAIRING": rc.UNKNOWN, "SUSPENDED": rc.UNKNOWN, "SUSPENDING": rc.UNKNOWN}


def azure_status(creds: Credentials, subscription: str, group: str, name: str):
    q = urllib.parse.quote
    url = (f"{ARM}subscriptions/{q(subscription)}/resourceGroups/{q(group)}/providers/Microsoft.Compute/"
           f"virtualMachines/{q(name)}/instanceView?api-version=2024-03-01")
    status, body = creds.http("GET", url, {"Authorization": "Bearer " + creds.azure_token(ARM)})
    if status != 200:
        raise CredentialError(f"Azure read failed (HTTP {status}): {(body.get('error') or {}).get('message', body)}")
    for s in body.get("statuses", []):
        code = s.get("code", "")
        if code.startswith("PowerState/"):
            return _AZURE.get(code.split("/", 1)[1], rc.UNKNOWN), code
    return rc.UNKNOWN, "no PowerState reported; statuses=" + ", ".join(
        s.get("code", "?") for s in body.get("statuses", [])) + "; keys=" + ",".join(sorted(body))


def gcp_status(creds: Credentials, project: str, zone: str, name: str):
    q = urllib.parse.quote
    url = f"https://compute.googleapis.com/compute/v1/projects/{q(project)}/zones/{q(zone)}/instances/{q(name)}"
    status, body = creds.http("GET", url, {"Authorization": "Bearer " + creds.gcp_token()})
    if status != 200:
        raise CredentialError(f"GCP read failed (HTTP {status}): {(body.get('error') or {}).get('message', body)}")
    raw = body.get("status", "")
    return _GCP.get(raw, rc.UNKNOWN), raw


def vm_status(creds: Credentials, provider: str, scope: str, group_or_zone: str, name: str):
    fn = azure_status if provider == "azure" else gcp_status
    return fn(creds, scope, group_or_zone, name)
