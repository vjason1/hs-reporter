"""Sign-in (HTTP Basic) for the GUI and API.

The username and password come from HSR_USER / HSR_PASSWORD, unless they've been set or
changed on the Settings page: then <data>/auth.json holds the username and a salted
PBKDF2-SHA256 hash of the password (never the password itself). Deleting auth.json goes back
to the environment variables, which is the way back in if the password is lost.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import threading

from . import store

PATH = store.DATA_DIR / "auth.json"
ITERATIONS = 200_000
MIN_LENGTH = 8
_lock = threading.RLock()
_verified: set[str] = set()   # digests of Authorization headers already checked against the hash


class AuthError(ValueError):
    pass


def _hash(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS).hex()


def _stored() -> dict | None:
    try:
        with open(PATH) as f:
            d = json.load(f)
        return d if d.get("username") and d.get("hash") and d.get("salt") else None
    except (OSError, json.JSONDecodeError):
        return None


def config() -> dict:
    """{'enabled', 'username', 'source': 'settings' | 'environment' | None}"""
    s = _stored()
    if s:
        return {"enabled": True, "username": s["username"], "source": "settings"}
    user = os.environ.get("HSR_USER")
    if user:
        return {"enabled": True, "username": user, "source": "environment"}
    return {"enabled": False, "username": None, "source": None}


def _password_ok(user: str, password: str) -> bool:
    s = _stored()
    if s:
        return (secrets.compare_digest(user, s["username"])
                and hmac.compare_digest(_hash(password, bytes.fromhex(s["salt"])), s["hash"]))
    env_user = os.environ.get("HSR_USER")
    if not env_user:
        return True
    return (secrets.compare_digest(user, env_user)
            and secrets.compare_digest(password, os.environ.get("HSR_PASSWORD") or ""))


def check_header(header: str | None) -> bool:
    """Is this Authorization header valid? The browser sends it with every request, so a
    successful check is remembered (as a digest) until the password changes."""
    if not config()["enabled"]:
        return True
    if not header or not header.lower().startswith("basic "):
        return False
    key = hashlib.sha256(header.encode()).hexdigest()
    if key in _verified:
        return True
    try:
        user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    if _password_ok(user, pw):
        with _lock:
            if len(_verified) > 100:
                _verified.clear()
            _verified.add(key)
        return True
    return False


def _save(username: str, password: str) -> None:
    salt = secrets.token_bytes(16)
    data = {"username": username, "salt": salt.hex(), "hash": _hash(password, salt),
            "algorithm": f"pbkdf2-sha256-{ITERATIONS}", "updated": store.now()}
    with _lock:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PATH.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, PATH)
        _verified.clear()   # every browser signs in again with the new password


def _check_new(password: str, confirm: str) -> None:
    if len(password or "") < MIN_LENGTH:
        raise AuthError(f"Use at least {MIN_LENGTH} characters for the new password")
    if password != confirm:
        raise AuthError("The new passwords don't match")


def change_password(current: str, new: str, confirm: str) -> dict:
    c = config()
    if not c["enabled"]:
        raise AuthError("Sign-in is off; turn it on with a username and password instead")
    if not _password_ok(c["username"], current or ""):
        raise AuthError("The current password isn't right")
    _check_new(new, confirm)
    if new == current:
        raise AuthError("The new password is the same as the current one")
    _save(c["username"], new)
    return config()


def enable(username: str, password: str, confirm: str) -> dict:
    if config()["enabled"]:
        raise AuthError("Sign-in is already on; change the password instead")
    username = (username or "").strip()
    if not username or ":" in username or any(ch.isspace() for ch in username):
        raise AuthError("Enter a username without spaces or colons")
    _check_new(password, confirm)
    _save(username, password)
    return config()
