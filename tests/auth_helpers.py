from __future__ import annotations

from uuid import UUID

from httpx import AsyncClient


async def signup_user(
    client: AsyncClient,
    email: str,
    password: str,
    plan: str | None = None,
) -> tuple[UUID, str, dict[str, object]]:
    payload: dict[str, object] = {"email": email, "password": password}
    if plan is not None:
        payload["plan"] = plan
    response = await client.post("/v1/auth/signup", json=payload)
    response.raise_for_status()
    data = response.json()
    user = data["user"]
    return UUID(str(user["id"])), str(data["access_token"]), data


async def login_user(
    client: AsyncClient,
    email: str,
    password: str,
) -> tuple[UUID, str, dict[str, object]]:
    response = await client.post("/v1/auth/login", json={"email": email, "password": password})
    response.raise_for_status()
    data = response.json()
    user = data["user"]
    return UUID(str(user["id"])), str(data["access_token"]), data


def bearer_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
