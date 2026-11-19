"""Local users: username + password, two roles. Passwords are stored as salted PBKDF2 hashes."""
import hashlib
import hmac
import secrets
from typing import List, Optional

from . import db
from .logs import utcnow_iso
from .service import Store, UserError

ROLES = ("admin", "cloudops", "reader")  # admin: global admin, everything; cloudops: operations, nothing disruptive; reader: look only
ITERATIONS = 200_000
MIN_PASSWORD = 8


def hash_password(password: str, salt: Optional[bytes] = None, iterations: int = ITERATIONS) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2${iterations}${salt.hex()}${dk.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, it, salt, want = stored.split("$")
        got = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(it)).hex()
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, want)


def _check_new(username: str, password: str, role: str) -> None:
    if not username.strip() or any(c.isspace() for c in username):
        raise UserError("username must not be empty or contain spaces")
    if role not in ROLES:
        raise UserError(f"role must be one of {ROLES}")
    if len(password) < MIN_PASSWORD:
        raise UserError(f"password must be at least {MIN_PASSWORD} characters")


def add_user(store: Store, username: str, password: str, role: str, must_change: bool = False) -> None:
    _check_new(username, password, role)
    with db.connect(store.home) as con:
        if con.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            raise UserError(f"user already exists: {username}")
        con.execute("INSERT INTO users (username, pw_hash, role, disabled, created_at, must_change) VALUES (?,?,?,0,?,?)",
                    (username, hash_password(password), role, utcnow_iso(), int(must_change)))
        store.audit(con, "USER_ADDED", username, role=role, temporary_password=must_change)


def _require(con, username: str):
    r = con.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not r:
        raise UserError(f"user not found: {username}")
    return r


def _other_active_admins(con, username: str) -> int:
    return con.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND disabled=0 AND username<>?",
                       (username,)).fetchone()[0]


def set_password(store: Store, username: str, password: str, must_change: bool = False) -> None:
    """must_change=True makes it a one-time password: the user has to choose their own at the next login."""
    if len(password) < MIN_PASSWORD:
        raise UserError(f"password must be at least {MIN_PASSWORD} characters")
    with db.connect(store.home) as con:
        u = _require(con, username)
        con.execute("UPDATE users SET pw_hash=?, must_change=? WHERE username=?",
                    (hash_password(password), int(must_change), u["username"]))
        store.audit(con, "USER_PASSWORD_CHANGED", u["username"], temporary_password=must_change)


def set_role(store: Store, username: str, role: str) -> None:
    if role not in ROLES:
        raise UserError(f"role must be one of {ROLES}")
    with db.connect(store.home) as con:
        u = _require(con, username)
        if u["role"] == "admin" and role != "admin" and not _other_active_admins(con, u["username"]):
            raise UserError("cannot demote the last active admin")
        con.execute("UPDATE users SET role=? WHERE username=?", (role, u["username"]))
        store.audit(con, "USER_ROLE_CHANGED", u["username"], old=u["role"], new=role)


def set_disabled(store: Store, username: str, disabled: bool) -> None:
    with db.connect(store.home) as con:
        u = _require(con, username)
        if disabled and u["role"] == "admin" and not _other_active_admins(con, u["username"]):
            raise UserError("cannot disable the last active admin")
        con.execute("UPDATE users SET disabled=? WHERE username=?", (int(disabled), u["username"]))
        store.audit(con, "USER_DISABLED" if disabled else "USER_ENABLED", u["username"])


def remove_user(store: Store, username: str) -> None:
    with db.connect(store.home) as con:
        u = _require(con, username)
        if u["role"] == "admin" and not _other_active_admins(con, u["username"]):
            raise UserError("cannot remove the last active admin")
        con.execute("DELETE FROM users WHERE username=?", (u["username"],))
        store.audit(con, "USER_REMOVED", u["username"])


def list_users(store: Store) -> List:
    with db.connect(store.home) as con:
        return con.execute("SELECT username, role, disabled, created_at, must_change FROM users ORDER BY username").fetchall()


def active_info(store: Store, username: str) -> Optional[dict]:
    """{'role', 'must_change'} of an enabled user, or None if the user was removed or disabled."""
    with db.connect(store.home) as con:
        u = con.execute("SELECT role, disabled, must_change FROM users WHERE username=?", (username,)).fetchone()
    return {"role": u["role"], "must_change": bool(u["must_change"])} if u and not u["disabled"] else None


def active_role(store: Store, username: str) -> Optional[str]:
    """Current role of an enabled user, or None if the user was removed or disabled."""
    with db.connect(store.home) as con:
        u = con.execute("SELECT role, disabled FROM users WHERE username=?", (username,)).fetchone()
    return u["role"] if u and not u["disabled"] else None


def authenticate(store: Store, username: str, password: str) -> Optional[dict]:
    """Returns {'username','role'} on success. Failed attempts are audited (never the password)."""
    with db.connect(store.home) as con:
        u = con.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        ok = bool(u) and not u["disabled"] and check_password(password, u["pw_hash"])
        if not u:  # same work for unknown users so timing does not reveal which names exist
            check_password(password, hash_password("x"))
        store.audit(con, "LOGIN_OK" if ok else "LOGIN_FAILED", username)
    return {"username": u["username"], "role": u["role"], "must_change": bool(u["must_change"])} if ok else None
