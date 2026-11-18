"""HTTPS certificate helpers. Only needed when you do not already have PEM files; they need `pip install cryptography`."""
import ipaddress
import os
from datetime import datetime, timedelta, timezone

from .service import UserError


def _crypto():
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives.serialization import pkcs12
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    except ImportError:
        raise UserError("this needs the 'cryptography' package: pip install cryptography") from None
    return x509, hashes, serialization, rsa, pkcs12, ExtendedKeyUsageOID, NameOID


def _write(folder: str, cert_pem: bytes, key_pem: bytes):
    os.makedirs(folder, exist_ok=True)
    crt, key = os.path.join(folder, "minipmc.crt"), os.path.join(folder, "minipmc.key")
    with open(crt, "wb") as f:
        f.write(cert_pem)
    with open(key, "wb") as f:
        f.write(key_pem)
    return crt, key


def make_self_signed(names: list, folder: str, days: int = 825):
    """A certificate valid for the given host names / IP addresses. Browsers warn until it is trusted on each PC."""
    if not names:
        raise UserError("give at least one host name or IP")
    x509, hashes, ser, rsa, _, eku, NameOID = _crypto()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    sans = []
    for n in names:
        try:
            sans.append(x509.IPAddress(ipaddress.ip_address(n)))
        except ValueError:
            sans.append(x509.DNSName(n))
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([eku.SERVER_AUTH]), critical=False)
            .sign(key, hashes.SHA256()))
    return _write(folder, cert.public_bytes(ser.Encoding.PEM),
                  key.private_bytes(ser.Encoding.PEM, ser.PrivateFormat.TraditionalOpenSSL, ser.NoEncryption()))


def pfx_to_pem(pfx_path: str, password: str, folder: str):
    """Split a .pfx/.p12 (what Windows and most CAs hand out) into the PEM certificate chain and key."""
    _, _, ser, _, pkcs12, _, _ = _crypto()
    try:
        with open(pfx_path, "rb") as f:
            key, cert, extra = pkcs12.load_key_and_certificates(f.read(), password.encode() or None)
    except (OSError, ValueError) as e:
        raise UserError(f"cannot read the pfx ({e}); check the path and password") from e
    if key is None or cert is None:
        raise UserError("the pfx has no private key or certificate")
    chain = cert.public_bytes(ser.Encoding.PEM) + b"".join(c.public_bytes(ser.Encoding.PEM) for c in extra or [])
    return _write(folder, chain, key.private_bytes(ser.Encoding.PEM, ser.PrivateFormat.TraditionalOpenSSL, ser.NoEncryption()))
