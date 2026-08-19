"""Who is allowed to open a console, and how that survives a page load.

Deliberately small and dependency free. The platform's API authentication is a
single shared key checked in ``sca.api.deps``; this is a different question —
not "is this call allowed" but "is there a person here" — and it answers only
for the HTML pages. Nothing in here changes what an API client has to send, so
scripts, the WhatsApp webhook and the tests are untouched.

Passwords are PBKDF2-SHA256 from the standard library rather than bcrypt or
argon2, and the session cookie is signed with ``hmac`` rather than
``itsdangerous``. Both of those would be better in a service holding real
credentials for thousands of people; for a fixed handful of staff accounts they
would be two dependencies to install on every host in exchange for margin
nobody here is close to needing.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

# Cost of a password check. 600k iterations is roughly a quarter of a second on
# a laptop, which is unnoticeable once per sign-in and expensive enough that a
# stolen .env is not a list of passwords by the weekend.
_ITERATIONS = 600_000
_ALGO = "pbkdf2_sha256"

COOKIE = "tgc_session"


def hash_password(password: str) -> str:
    """The string that goes in the environment. Never the password itself."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"{_ALGO}${_ITERATIONS}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """False for a wrong password and false for a malformed record, so a hash
    somebody truncated while editing .env fails closed rather than open."""
    try:
        algo, rounds, salt, digest = stored.split("$")
        if algo != _ALGO:
            return False
        expected = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), _unb64(salt), int(rounds)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(expected, _unb64(digest))


def parse_users(raw: str) -> dict[str, str]:
    """``email:hash,email:hash`` from the environment into a lookup.

    Addresses are lowercased, because somebody typing their own address with a
    capital on a Monday is not a different person. Blank and malformed entries
    are dropped rather than raising: a stray comma at the end of the line should
    not stop the service booting.
    """
    users: dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        email, _, stored = entry.partition(":")
        email, stored = email.strip().lower(), stored.strip()
        if email and stored:
            users[email] = stored
    return users


def authenticate(email: str, password: str, users: dict[str, str]) -> str | None:
    """The signed-in address, or None.

    An unknown address is checked against a throwaway hash rather than returning
    early, so the time this takes does not say which addresses exist.
    """
    email = email.strip().lower()
    stored = users.get(email)
    if stored is None:
        verify_password(password, _DECOY)
        return None
    return email if verify_password(password, stored) else None


def issue(email: str, secret: str, hours: int) -> str:
    """A signed statement that this address signed in, and when it stops
    counting. Nothing is stored server side — there is no session table to
    outlive a restart, and equally no way to revoke one before it expires, which
    is the trade being made. Shortening the lifetime is the lever."""
    body = _b64(json.dumps({"sub": email, "exp": int(time.time()) + hours * 3600}).encode())
    return f"{body}.{_sign(body, secret)}"


def read(token: str | None, secret: str) -> str | None:
    """The address inside a cookie, if the cookie is ours and still current."""
    if not token or "." not in token:
        return None
    body, _, signature = token.partition(".")
    if not hmac.compare_digest(signature, _sign(body, secret)):
        return None
    try:
        claims = json.loads(_unb64(body))
        if int(claims["exp"]) < time.time():
            return None
        return str(claims["sub"])
    except (ValueError, KeyError, TypeError):
        return None


def _sign(body: str, secret: str) -> str:
    return _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


# Costs the same to check as a real record, so a sign-in attempt against an
# address that does not exist takes as long as one against an address that does.
_DECOY = hash_password(secrets.token_urlsafe(16))
