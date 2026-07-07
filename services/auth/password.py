from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets

_HASH_NAME = "pbkdf2_sha256"
_DEFAULT_ITERATIONS = 600_000
_SALT_BYTES = 16
_DERIVED_KEY_BYTES = 32


def hash_password(password: str, *, iterations: int = _DEFAULT_ITERATIONS) -> str:
    salt = secrets.token_bytes(_SALT_BYTES)
    derived_key = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
        dklen=_DERIVED_KEY_BYTES,
    )
    salt_text = base64.b64encode(salt).decode("ascii")
    hash_text = base64.b64encode(derived_key).decode("ascii")
    return f"{_HASH_NAME}${iterations}${salt_text}${hash_text}"


def verify_password(password: str, password_hash: str) -> bool:
    parts = password_hash.split("$")
    if len(parts) != 4 or parts[0] != _HASH_NAME:
        return False
    try:
        iterations = int(parts[1])
        salt = base64.b64decode(parts[2], validate=True)
        expected = base64.b64decode(parts[3], validate=True)
    except (ValueError, TypeError, binascii.Error):
        return False
    derived_key = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
        dklen=len(expected),
    )
    return hmac.compare_digest(derived_key, expected)
