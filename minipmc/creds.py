"""Cloud credentials, standard library only (no Azure/Google SDKs, no compiled packages).

Azure: a user-assigned managed identity (UAMI) gets tokens from the VM's metadata service.
GCP:   the service-account JSON key is read from Azure Key Vault (using that same identity), and exchanged for
       a Google access token with a signed JWT (RS256, implemented here with Python's big-integer math).
The key is only held in memory; it is never written to disk or logged.
"""
import base64
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

IMDS = "http://169.254.169.254/metadata/identity/oauth2/token"
ARM = "https://management.azure.com/"
VAULT = "https://vault.azure.net"
GCP_SCOPE = "https://www.googleapis.com/auth/compute"
SETTINGS_FILE = os.path.join("config", "settings.json")
THROTTLE_RETRIES = 3  # extra attempts after an HTTP 429 (or a 503 on a read)
SECRET_NAME = re.compile(r"[0-9A-Za-z-]{1,127}")  # Key Vault's rule for secret names

# DER prefix of DigestInfo for SHA-256 (RFC 8017, EMSA-PKCS1-v1_5)
_SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


class CredentialError(Exception):
    pass


def load_settings(home: str) -> dict:
    path = os.path.join(home, SETTINGS_FILE)
    if not os.path.exists(path):
        raise CredentialError(f"settings file not found: {path} (copy config\\settings.example.json)")
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


# ---------------------------------------------------------------- HTTP

def request(method, url, headers=None, data=None, timeout=20, proxy=True):
    """Returns (status, parsed JSON or {}). HTTP errors are returned, not raised; network errors raise."""
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    opener = urllib.request.build_opener() if proxy else urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for attempt in range(THROTTLE_RETRIES + 1):
        try:
            with opener.open(req, timeout=timeout) as r:
                status, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
            # 429 means the call was not processed, so retrying is safe for any method; 503 only for reads
            if attempt < THROTTLE_RETRIES and (status == 429 or (status == 503 and method == "GET")):
                try:
                    wait = float(e.headers.get("Retry-After", ""))
                except (TypeError, ValueError):
                    wait = 2.0 * (attempt + 1)
                time.sleep(min(max(wait, 0.5), 30.0))
                continue
        except urllib.error.URLError as e:
            raise CredentialError(f"cannot reach {urllib.parse.urlparse(url).netloc}: {e.reason}")
        break
    try:
        return status, json.loads(raw or b"{}")
    except ValueError:
        return status, {"raw": raw[:300].decode("utf-8", "replace")}


# ---------------------------------------------------------------- RS256 without extra packages

def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _der(buf: bytes, pos: int = 0):
    """Read one DER element -> (tag, content, next_position)."""
    tag, ln = buf[pos], buf[pos + 1]
    pos += 2
    if ln & 0x80:
        k = ln & 0x7F
        ln = int.from_bytes(buf[pos:pos + k], "big")
        pos += k
    return tag, buf[pos:pos + ln], pos + ln


def _children(buf: bytes):
    pos, out = 0, []
    while pos < len(buf):
        tag, content, pos = _der(buf, pos)
        out.append((tag, content))
    return out


def parse_private_key(pem: str):
    """PKCS#8 ('BEGIN PRIVATE KEY', what Google issues) or PKCS#1 PEM -> (n, d)."""
    lines = [ln.strip() for ln in pem.strip().splitlines()]
    if not lines or "BEGIN" not in lines[0]:
        raise CredentialError("private key is not in PEM format")
    try:
        der = base64.b64decode("".join(ln for ln in lines if not ln.startswith("-----")))
        _, top, _ = _der(der)
        parts = _children(top)
        if lines[0] == "-----BEGIN PRIVATE KEY-----":  # PKCS#8: version, algorithm, octet string holding PKCS#1
            _, top, _ = _der(parts[2][1])
            parts = _children(top)
        return int.from_bytes(parts[1][1], "big"), int.from_bytes(parts[3][1], "big")
    except (IndexError, ValueError):
        raise CredentialError("could not read the private key (unsupported or damaged)")


def rs256_sign(message: bytes, n: int, d: int) -> bytes:
    k = (n.bit_length() + 7) // 8
    t = _SHA256_PREFIX + hashlib.sha256(message).digest()
    em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return pow(int.from_bytes(em, "big"), d, n).to_bytes(k, "big")


def make_jwt(info: dict, scope: str, now: float) -> str:
    n, d = parse_private_key(info["private_key"])
    head = _b64u(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    uri = info.get("token_uri", "https://oauth2.googleapis.com/token")
    claims = _b64u(json.dumps({"iss": info["client_email"], "scope": scope, "aud": uri,
                               "iat": int(now), "exp": int(now) + 3600}).encode())
    signing_input = f"{head}.{claims}".encode()
    return f"{head}.{claims}.{_b64u(rs256_sign(signing_input, n, d))}"


# ---------------------------------------------------------------- credentials

class Credentials:
    def __init__(self, settings: dict, http=request, clock=time.time):
        self.cfg = settings
        self.http, self.clock = http, clock
        self._tokens = {}  # key -> (token, expires_epoch)
        self._gcp_info = None

    def _cached(self, key):
        t = self._tokens.get(key)
        return t[0] if t and t[1] - 120 > self.clock() else None

    def azure_token(self, resource: str = ARM) -> str:
        """Token for the user-assigned managed identity (client_id from settings)."""
        key = "az:" + resource
        t = self._cached(key)
        if t:
            return t
        client_id = self.cfg.get("azure", {}).get("uami_client_id", "").strip()
        if not client_id:
            raise CredentialError("settings: azure.uami_client_id is empty")
        q = urllib.parse.urlencode({"api-version": "2018-02-01", "resource": resource, "client_id": client_id})
        status, body = self.http("GET", f"{IMDS}?{q}", {"Metadata": "true"}, timeout=10, proxy=False)
        if status != 200 or "access_token" not in body:
            why = body.get("error_description") or body.get("error") or body
            raise CredentialError(f"managed identity token failed (HTTP {status}): {why}. "
                                  "Is the identity assigned to this VM, and is the client id right?")
        self._tokens[key] = (body["access_token"], self.clock() + int(body.get("expires_in", 3600)))
        return body["access_token"]

    def keyvault_secret(self, name: str) -> str:
        vault = self.cfg.get("azure", {}).get("key_vault_url", "").rstrip("/")
        if not vault.startswith("https://"):
            raise CredentialError("settings: azure.key_vault_url must look like https://<name>.vault.azure.net")
        if not SECRET_NAME.fullmatch(name):
            raise CredentialError(f"Key Vault secret name {name!r} is invalid: use only letters, digits and hyphens "
                                  "(1-127 characters), and put the secret NAME here, not its value or the URL")
        status, body = self.http("GET", f"{vault}/secrets/{urllib.parse.quote(name)}?api-version=7.4",
                                 {"Authorization": "Bearer " + self.azure_token(VAULT)})
        if status != 200 or "value" not in body:
            hint = {401: "token rejected", 404: "secret name not found",
                    403: "the identity lacks 'Key Vault Secrets User' (or the vault firewall blocks this VM)"}.get(status, "")
            err = body.get("error")
            msg = (err.get("message") if isinstance(err, dict) else "") or ""
            if msg and status not in (401, 403, 404):
                hint = (hint + " " + msg[:150]).strip()
            raise CredentialError(f"Key Vault read failed (HTTP {status}) {hint}".strip())
        return body["value"]

    def gcp_info(self) -> dict:
        if self._gcp_info is None:
            name = self.cfg.get("gcp", {}).get("key_secret_name", "").strip()
            if not name:
                raise CredentialError("settings: gcp.key_secret_name is empty")
            try:
                info = json.loads(self.keyvault_secret(name))
                info["client_email"], info["private_key"]
            except (ValueError, KeyError, TypeError):
                raise CredentialError("the Key Vault secret is not a Google service-account JSON key file")
            self._gcp_info = info
        return self._gcp_info

    def gcp_token(self) -> str:
        t = self._cached("gcp")
        if t:
            return t
        info = self.gcp_info()
        assertion = make_jwt(info, GCP_SCOPE, self.clock())
        body = urllib.parse.urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                       "assertion": assertion}).encode()
        status, resp = self.http("POST", info.get("token_uri", "https://oauth2.googleapis.com/token"),
                                 {"Content-Type": "application/x-www-form-urlencoded"}, body)
        if status != 200 or "access_token" not in resp:
            why = resp.get("error_description") or resp.get("error") or resp
            raise CredentialError(f"Google token request failed (HTTP {status}): {why}")
        self._tokens["gcp"] = (resp["access_token"], self.clock() + int(resp.get("expires_in", 3600)))
        return resp["access_token"]
