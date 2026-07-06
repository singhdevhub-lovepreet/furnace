from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import HTTPConnection

from services.auth.jwt import decode_access_token
from services.db.models import User


def _bearer_token(authorization: str | None) -> str:
    if authorization is None:
        raise HTTPException(
            status_code=401,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=401,
            detail="invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token.strip()


async def _load_user(db: AsyncSession, user_id: UUID) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


async def resolve_user_from_token_string(connection: HTTPConnection, token: str) -> User:
    settings = connection.app.state.settings
    secret = settings.auth_jwt_secret
    if secret is None:
        raise RuntimeError("auth_jwt_secret must be configured")
    try:
        user_id = decode_access_token(secret, token)
    except ValueError as exc:
        raise HTTPException(
            status_code=401,
            detail="invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    sessionmaker = connection.app.state.sessionmaker
    async with sessionmaker() as db:
        return await _load_user(db, user_id)


async def get_current_user(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> User:
    token = _bearer_token(authorization)
    return await resolve_user_from_token_string(request, token)
