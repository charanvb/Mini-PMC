"""VM status lookups and start/stop calls (Azure Resource Manager and Google Compute)."""
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
           f"virtualMachines/{q(name)}?$expand=instanceView&api-version=2024-03-01")
    status, body = creds.http("GET", url, {"Authorization": "Bearer " + creds.azure_token(ARM)})
    if status != 200:
        raise CredentialError(f"Azure read failed (HTTP {status}): {(body.get('error') or {}).get('message', body)}")
    # needs only virtualMachines/read; the statuses sit under properties.instanceView
    body = (body.get("properties") or {}).get("instanceView") or body
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


class ProviderError(CredentialError):
    """A start/stop call the cloud refused. `status` is the HTTP status (None if the call never got an answer)."""

    def __init__(self, message: str, status=None):
        super().__init__(message)
        self.status = status


def vm_action(creds: Credentials, provider: str, scope: str, group_or_zone: str, name: str, action: str) -> str:
    """Ask the cloud to start or stop one VM (Azure stop = deallocate). Returns once the request is accepted."""
    q = urllib.parse.quote
    if provider == "azure":
        verb = "start" if action == "start" else "deallocate"
        url = (f"{ARM}subscriptions/{q(scope)}/resourceGroups/{q(group_or_zone)}/providers/Microsoft.Compute/"
               f"virtualMachines/{q(name)}/{verb}?api-version=2024-03-01")
        headers = {"Authorization": "Bearer " + creds.azure_token(ARM)}
    else:
        verb = "start" if action == "start" else "stop"
        url = (f"https://compute.googleapis.com/compute/v1/projects/{q(scope)}/zones/{q(group_or_zone)}/"
               f"instances/{q(name)}/{verb}")
        headers = {"Authorization": "Bearer " + creds.gcp_token()}
    status, body = creds.http("POST", url, headers, b"")  # empty body: ARM insists on Content-Length: 0
    if status not in (200, 202):
        err = body.get("error")
        msg = err.get("message") if isinstance(err, dict) else (err or body.get("raw") or "")
        raise ProviderError(f"{verb} refused (HTTP {status}): {str(msg)[:200]}", status)
    return f"HTTP {status}"


def _azure_power(statuses):
    for s in statuses or []:
        code = s.get("code", "")
        if code.startswith("PowerState/"):
            return _AZURE.get(code.split("/", 1)[1], rc.UNKNOWN), code
    return None


def azure_list(creds: Credentials, subscription: str) -> dict:
    """Power state of every VM in a subscription in a few calls. {(resource group, name) lower-cased: (state, raw)}.

    VMs whose power state is not in the answer are left out; the caller reads those one by one."""
    q = urllib.parse.quote
    url = (f"{ARM}subscriptions/{q(subscription)}/providers/Microsoft.Compute/virtualMachines"
           f"?statusOnly=true&api-version=2024-03-01")
    headers = {"Authorization": "Bearer " + creds.azure_token(ARM)}
    out, pages = {}, 0
    while url and pages < 200:
        status, body = creds.http("GET", url, headers)
        if status != 200:
            raise CredentialError(f"Azure list failed (HTTP {status}): {(body.get('error') or {}).get('message', body)}")
        for item in body.get("value", []):
            props = item.get("properties") or {}
            view = props.get("instanceView") or props
            found = _azure_power(view.get("statuses") or item.get("statuses"))
            parts = (item.get("id") or "").split("/")
            if found and len(parts) > 8 and parts[3].lower() == "resourcegroups":
                out[(parts[4].lower(), (item.get("name") or parts[-1]).lower())] = found
        url, pages = body.get("nextLink"), pages + 1
    if url:
        raise CredentialError("Azure list has more than 200 pages; refusing to use a partial answer")
    return out


def gcp_list(creds: Credentials, project: str) -> dict:
    """Status of every instance in a project. {(zone, name) lower-cased: (state, raw)}."""
    q = urllib.parse.quote
    base = (f"https://compute.googleapis.com/compute/v1/projects/{q(project)}/aggregated/instances"
            f"?maxResults=500&fields=nextPageToken,items/*/instances(name,status)")
    headers = {"Authorization": "Bearer " + creds.gcp_token()}
    out, token, pages = {}, "", 0
    while pages < 200:
        status, body = creds.http("GET", base + (f"&pageToken={q(token)}" if token else ""), headers)
        if status != 200:
            raise CredentialError(f"GCP list failed (HTTP {status}): {(body.get('error') or {}).get('message', body)}")
        for key, part in (body.get("items") or {}).items():
            zone = key.split("/", 1)[-1].lower()
            for inst in part.get("instances", []):
                raw = inst.get("status", "")
                out[(zone, (inst.get("name") or "").lower())] = (_GCP.get(raw, rc.UNKNOWN), raw)
        token, pages = body.get("nextPageToken", ""), pages + 1
        if not token:
            break
    else:
        raise CredentialError("GCP list has more than 200 pages; refusing to use a partial answer")
    return out


def list_scope(creds: Credentials, provider: str, scope: str) -> dict:
    return (azure_list if provider == "azure" else gcp_list)(creds, scope)
