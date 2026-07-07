from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
from jwt import ExpiredSignatureError, InvalidTokenError


def create_access_token(secret: str, user_id: UUID, ttl_seconds: int) -> str:
    now = datetime.now(UTC)
    payload: dict[str, object] = {
        "sub": str(user_id),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl_seconds)).timestamp()),
    }
    token = jwt.encode(payload, secret, algorithm="HS256")
    if not isinstance(token, str):
        raise RuntimeError("failed to encode JWT access token")
    return token


def decode_access_token(secret: str, token: str) -> UUID:
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
    except ExpiredSignatureError as exc:
        raise ValueError("token expired") from exc
    except InvalidTokenError as exc:
        raise ValueError("invalid token") from exc
    subject = payload.get("sub")
    if not isinstance(subject, str):
        raise ValueError("invalid token")
    try:
        return UUID(subject)
    except ValueError as exc:
        raise ValueError("invalid token") from exc
