"""Authentication for the internal agent console.

Deliberately built on the standard library. The console is one shared login for
a handful of staff, and adding `passlib`/`bcrypt`/`itsdangerous` to the image for
that is weight the Docker build does not need. `hashlib.scrypt` is a memory-hard
KDF built into Python and is the right tool here; HMAC-signed cookies are the
same mechanism every session library uses underneath.

What this protects matters: the console shows every customer conversation and
every phone number the bot has ever spoken to. That is personal data, so the
posture is the same as the rest of the app - refuse to run unconfigured in
production rather than fall back to something permissive.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

from app.core.logging import get_logger

logger = get_logger(__name__)

#: scrypt parameters. n=2**14 keeps a single login around 50-100ms on Render's
#: free CPU - slow enough to make guessing expensive, fast enough that logging
#: in does not feel broken.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_LEN = 32

#: How long a console session lasts. Short enough that a laptop left open in an
#: office does not stay authenticated for a week.
SESSION_TTL_SECONDS = 12 * 60 * 60


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Hash a password for storage. Returns `scrypt$<salt_b64>$<hash_b64>`."""
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_KEY_LEN,
    )
    return f"scrypt${_b64(salt)}${_b64(derived)}"


def verify_password(password: str, stored: str) -> bool:
    """Check a password against a stored hash. False on anything malformed.

    Never raises: a corrupted or hand-edited env var must fail the login, not
    crash the endpoint into a 500 that leaks a stack trace.
    """
    try:
        scheme, salt_b64, hash_b64 = stored.split("$", 2)
        if scheme != "scrypt":
            return False
        salt = _unb64(salt_b64)
        expected = _unb64(hash_b64)
    except (ValueError, TypeError):
        logger.warning("CONSOLE_PASSWORD_HASH is malformed - refusing the login")
        return False

    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=len(expected),
    )
    # Constant-time: a timing side channel here would leak the hash byte by byte.
    return hmac.compare_digest(derived, expected)


def issue_session(username: str, secret: str) -> str:
    """Signed session token: `<payload_b64>.<hmac_b64>`.

    The payload is readable by design - it holds only a username and an expiry,
    nothing secret. The signature is what makes it unforgeable, and the expiry
    is inside the signed payload so it cannot be extended by editing the cookie.
    """
    payload = {"u": username, "exp": int(time.time()) + SESSION_TTL_SECONDS}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return f"{_b64(raw)}.{_b64(_sign(raw, secret))}"


def read_session(token: str, secret: str) -> str | None:
    """Username from a valid, unexpired token. None otherwise."""
    if not token or not secret:
        return None
    try:
        payload_b64, signature_b64 = token.split(".", 1)
        raw = _unb64(payload_b64)
        signature = _unb64(signature_b64)
    except (ValueError, TypeError):
        return None

    if not hmac.compare_digest(_sign(raw, secret), signature):
        return None

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None

    if int(payload.get("exp", 0)) < time.time():
        return None
    username = payload.get("u")
    return str(username) if username else None


def _sign(raw: bytes, secret: str) -> bytes:
    return hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    # urlsafe_b64decode is strict about padding; tokens carry none.
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
