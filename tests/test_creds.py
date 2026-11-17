import base64
import json
import random
from urllib.parse import parse_qs, urlparse

import pytest

from minipmc import creds as cr, providers, reconcile as rc


# ---- tiny RSA key generator + DER writer so the signer can be verified without any packages
def _is_prime(n, rnd):
    if n % 2 == 0:
        return False
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(20):
        x = pow(rnd.randrange(2, n - 2), d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits, rnd):
    while True:
        c = rnd.getrandbits(bits) | (1 << (bits - 1)) | 1
        if _is_prime(c, rnd):
            return c


def _tlv(tag, content):
    n = len(content)
    ln = bytes([n]) if n < 128 else bytes([0x80 | (n.bit_length() + 7) // 8]) + n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([tag]) + ln + content


def _int(v):
    b = v.to_bytes((v.bit_length() + 8) // 8, "big")
    return _tlv(2, b)


@pytest.fixture(scope="module")
def key():
    rnd = random.Random(7)
    while True:
        p, q = _prime(512, rnd), _prime(512, rnd)
        e = 65537
        phi = (p - 1) * (q - 1)
        if p != q and phi % e:
            break
    n, d = p * q, pow(e, -1, phi)
    pkcs1 = _tlv(0x30, b"".join(_int(x) for x in (0, n, e, d, p, q, d % (p - 1), d % (q - 1), pow(q, -1, p))))
    alg = _tlv(0x30, _tlv(6, bytes.fromhex("2a864886f70d010101")) + b"\x05\x00")
    pkcs8 = _tlv(0x30, _int(0) + alg + _tlv(4, pkcs1))

    def pem(label, der):
        b = base64.b64encode(der).decode()
        return f"-----BEGIN {label}-----\n" + "\n".join(b[i:i + 64] for i in range(0, len(b), 64)) + f"\n-----END {label}-----\n"

    return {"n": n, "e": e, "d": d, "pkcs8": pem("PRIVATE KEY", pkcs8), "pkcs1": pem("RSA PRIVATE KEY", pkcs1)}


def test_private_key_formats_parse(key):
    assert cr.parse_private_key(key["pkcs8"]) == (key["n"], key["d"])
    assert cr.parse_private_key(key["pkcs1"]) == (key["n"], key["d"])
    with pytest.raises(cr.CredentialError):
        cr.parse_private_key("not a key")


def test_jwt_signature_verifies(key):
    info = {"client_email": "sa@p.iam.gserviceaccount.com", "private_key": key["pkcs8"]}
    jwt = cr.make_jwt(info, cr.GCP_SCOPE, 1_000_000)
    head, claims, sig = jwt.split(".")
    pad = lambda s: s + "=" * (-len(s) % 4)
    assert json.loads(base64.urlsafe_b64decode(pad(head))) == {"alg": "RS256", "typ": "JWT"}
    c = json.loads(base64.urlsafe_b64decode(pad(claims)))
    assert c["iss"] == info["client_email"] and c["exp"] - c["iat"] == 3600
    k = (key["n"].bit_length() + 7) // 8
    em = pow(int.from_bytes(base64.urlsafe_b64decode(pad(sig)), "big"), key["e"], key["n"]).to_bytes(k, "big")
    import hashlib
    t = cr._SHA256_PREFIX + hashlib.sha256(f"{head}.{claims}".encode()).digest()
    assert em == b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t


class FakeHttp:
    def __init__(self, key_json, vault_status=200):
        self.calls, self.key_json, self.vault_status = [], key_json, vault_status

    def __call__(self, method, url, headers=None, data=None, timeout=20, proxy=True):
        self.calls.append((method, url, headers))
        u = urlparse(url)
        if u.netloc == "169.254.169.254":
            q = parse_qs(u.query)
            assert q["client_id"] == ["uami-123"] and headers == {"Metadata": "true"} and proxy is False
            return 200, {"access_token": "AZ-" + q["resource"][0], "expires_in": 3600}
        if u.netloc == "kv.vault.azure.net":
            assert headers["Authorization"] == "Bearer AZ-https://vault.azure.net"
            return (200, {"value": self.key_json}) if self.vault_status == 200 else (self.vault_status, {})
        if u.netloc == "oauth2.googleapis.com":
            assert b"assertion=" in data
            return 200, {"access_token": "GCP-TOKEN", "expires_in": 3600}
        if u.netloc == "management.azure.com":
            return 200, {"statuses": [{"code": "ProvisioningState/succeeded"}, {"code": "PowerState/deallocated"}]}
        if u.netloc == "compute.googleapis.com":
            assert headers["Authorization"] == "Bearer GCP-TOKEN"
            return 200, {"status": "TERMINATED"}
        raise AssertionError(url)


SETTINGS = {"azure": {"uami_client_id": "uami-123", "key_vault_url": "https://kv.vault.azure.net"},
            "gcp": {"key_secret_name": "gcp-key"}}


def test_full_chain_and_token_caching(key):
    info = json.dumps({"client_email": "sa@x", "private_key": key["pkcs8"]})
    http = FakeHttp(info)
    c = cr.Credentials(SETTINGS, http=http)
    assert c.gcp_token() == "GCP-TOKEN"
    assert c.gcp_token() == "GCP-TOKEN"
    assert sum("oauth2.googleapis.com" in u for _, u, _ in http.calls) == 1  # second call used the cache
    assert providers.vm_status(c, "gcp", "proj", "europe-west4-a", "vm1") == (rc.STOPPED, "TERMINATED")
    assert providers.vm_status(c, "azure", "sub", "rg", "vm1") == (rc.DEALLOCATED, "PowerState/deallocated")


def test_clear_errors(key):
    with pytest.raises(cr.CredentialError, match="Secrets User"):
        cr.Credentials(SETTINGS, http=FakeHttp("{}", 403)).gcp_info()
    with pytest.raises(cr.CredentialError, match="not a Google"):
        cr.Credentials(SETTINGS, http=FakeHttp("{}")).gcp_info()
    with pytest.raises(cr.CredentialError, match="uami_client_id"):
        cr.Credentials({}, http=FakeHttp("{}")).azure_token()
