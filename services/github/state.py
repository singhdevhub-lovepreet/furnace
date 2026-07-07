from __future__ import annotations

import hmac
import secrets
from hashlib import sha256
from uuid import UUID


def build_install_state(secret: str, user_id: UUID) -> str:
    nonce = secrets.token_urlsafe(32)
    payload = f"{user_id}.{nonce}"
    digest = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), sha256).hexdigest()
    return f"{payload}.{digest}"


def verify_install_state(secret: str, state: str) -> UUID | None:
    parts = state.split(".")
    if len(parts) != 3:
        return None
    user_id_text, nonce, digest = parts
    payload = f"{user_id_text}.{nonce}"
    expected = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), sha256).hexdigest()
    if not hmac.compare_digest(expected, digest):
        return None
    try:
        return UUID(user_id_text)
    except ValueError:
        return None
