from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from services.app import create_app
from services.config import Settings
from services.db.models import Event, GithubInstallation, Repo, User
from services.scheduler.pool import PoolController
from services.scheduler.provisioner.base import SessionSpec
from services.scheduler.provisioner.fake import FakeProvisioner
from services.sessions.state_machine import SessionStatus


@pytest_asyncio.fixture
async def pool_app(tmp_path: Path) -> AsyncGenerator[FastAPI, None]:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'pool.db'}",
        artifacts_dir=str(tmp_path / "artifacts"),
        auto_create_schema=True,
        fake_max_slots=1,
        pool_capacity_override=1,
        pool_estimated_session_seconds=10,
        pool_scale_up_threshold=1,
        session_step_delay_seconds=0.01,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        yield app


async def seed_repo(app: FastAPI) -> UUID:
    sessionmaker = app.state.sessionmaker
    async with sessionmaker() as db:
        user = User(email="pool@example.com", plan="pro")
        installation = GithubInstallation(user=user, installation_id=1234, account_login="octo")
        repo = Repo(installation=installation, full_name="octo/repo", default_branch="main")
        db.add_all([user, installation, repo])
        await db.commit()
        await db.refresh(repo)
        return repo.id


async def wait_for_event_type(app: FastAPI, session_id: UUID, event_type: str) -> None:
    deadline = asyncio.get_running_loop().time() + 5.0
    sessionmaker = app.state.sessionmaker
    while True:
        async with sessionmaker() as db:
            rows = (
                (
                    await db.execute(
                        select(Event).where(Event.session_id == session_id).order_by(Event.ts)
                    )
                )
                .scalars()
                .all()
            )
            if any(event.type == event_type for event in rows):
                return
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for {event_type}")
        await asyncio.sleep(0.02)


async def wait_for_status(
    client: AsyncClient, session_id: UUID, target: SessionStatus
) -> SessionStatus:
    deadline = asyncio.get_running_loop().time() + 5.0
    while True:
        response = await client.get(f"/v1/sessions/{session_id}")
        response.raise_for_status()
        status = SessionStatus(response.json()["status"])
        if status == target:
            return status
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for {target}")
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_pool_controller_admits_immediately_then_queues_with_eta() -> None:
    provisioner = FakeProvisioner(max_slots=1)
    controller = PoolController(
        provisioner=provisioner,
        capacity_override=1,
        estimated_session_seconds=10,
        scale_up_threshold=1,
    )
    spec = SessionSpec(
        image_id="sample-image",
        cpu=4,
        memory_hint_mb=4096,
        ttl_seconds=60,
        idle_timeout_seconds=30,
    )
    session_one = uuid4()
    session_two = uuid4()
    events: list[tuple[int, int]] = []

    handle_one = await controller.acquire(session_one, spec)
    second_task = asyncio.create_task(
        controller.acquire(
            session_two,
            spec,
            on_queued=lambda eta, position: _capture_queue(events, eta, position),
        )
    )
    await asyncio.sleep(0)
    snapshot = await controller.snapshot()
    assert snapshot.active_sessions == 1
    assert snapshot.queue_depth == 1
    assert snapshot.queued[0].session_id == session_two
    assert snapshot.queued[0].position == 1
    assert snapshot.queued[0].eta_seconds == 10
    assert events == [(10, 1)]

    await controller.release(session_one, handle_one)
    handle_two = await second_task
    assert handle_two.id != handle_one.id


@pytest.mark.asyncio
async def test_pool_controller_releases_fifo_and_admits_next() -> None:
    provisioner = FakeProvisioner(max_slots=1)
    controller = PoolController(
        provisioner=provisioner,
        capacity_override=1,
        estimated_session_seconds=10,
        scale_up_threshold=1,
    )
    spec = SessionSpec(
        image_id="sample-image",
        cpu=4,
        memory_hint_mb=4096,
        ttl_seconds=60,
        idle_timeout_seconds=30,
    )
    s1, s2, s3 = uuid4(), uuid4(), uuid4()
    h1 = await controller.acquire(s1, spec)
    t2 = asyncio.create_task(controller.acquire(s2, spec))
    t3 = asyncio.create_task(controller.acquire(s3, spec))
    await asyncio.sleep(0)
    await controller.release(s1, h1)
    h2 = await t2
    assert not t3.done()
    await controller.release(s2, h2)
    h3 = await t3
    assert h3.id not in {h1.id, h2.id}


@pytest.mark.asyncio
async def test_pool_controller_cancel_while_queued_removes_without_leak() -> None:
    provisioner = FakeProvisioner(max_slots=1)
    controller = PoolController(
        provisioner=provisioner,
        capacity_override=1,
        estimated_session_seconds=10,
        scale_up_threshold=1,
    )
    spec = SessionSpec(
        image_id="sample-image",
        cpu=4,
        memory_hint_mb=4096,
        ttl_seconds=60,
        idle_timeout_seconds=30,
    )
    s1, s2 = uuid4(), uuid4()
    h1 = await controller.acquire(s1, spec)
    queued = asyncio.create_task(controller.acquire(s2, spec))
    await asyncio.sleep(0)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    snapshot = await controller.snapshot()
    assert snapshot.queue_depth == 0
    assert snapshot.active_sessions == 1
    await controller.release(s1, h1)
    snapshot = await controller.snapshot()
    assert snapshot.active_sessions == 0
    assert snapshot.capacity == 1


@pytest.mark.asyncio
async def test_pool_controller_cancel_after_reserved_does_not_leak_slot() -> None:
    provisioner = FakeProvisioner(max_slots=1)
    controller = PoolController(
        provisioner=provisioner,
        capacity_override=1,
        estimated_session_seconds=10,
        scale_up_threshold=1,
    )
    spec = SessionSpec(
        image_id="sample-image",
        cpu=4,
        memory_hint_mb=4096,
        ttl_seconds=60,
        idle_timeout_seconds=30,
    )
    s1, s2, s3 = uuid4(), uuid4(), uuid4()
    h1 = await controller.acquire(s1, spec)
    queued = asyncio.create_task(controller.acquire(s2, spec))
    await asyncio.sleep(0)
    # release() drains the queue, reserving s2 and resolving its future, but s2's
    # coroutine has not resumed yet. Cancelling it here exercises the reserve-then-cancel
    # race: the reservation must be dropped so the freed slot is not leaked.
    await controller.release(s1, h1)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    snapshot = await controller.snapshot()
    assert snapshot.active_sessions == 0
    assert snapshot.queue_depth == 0
    # The slot must still be usable; a leak would keep this acquire queued forever.
    h3 = await asyncio.wait_for(controller.acquire(s3, spec), timeout=2.0)
    assert h3.id != h1.id


@pytest.mark.asyncio
async def test_pool_controller_reconcile_repopulates_active_handles() -> None:
    provisioner = FakeProvisioner(max_slots=2)
    spec = SessionSpec(
        image_id="sample-image",
        cpu=4,
        memory_hint_mb=4096,
        ttl_seconds=60,
        idle_timeout_seconds=30,
    )
    existing = await provisioner.acquire(spec)
    controller = PoolController(
        provisioner=provisioner,
        capacity_override=2,
        estimated_session_seconds=10,
        scale_up_threshold=1,
    )
    handles = await controller.reconcile()
    assert [handle.id for handle in handles] == [existing.handle.id]
    snapshot = await controller.snapshot()
    assert snapshot.active_sessions == 1


@pytest.mark.asyncio
async def test_pool_controller_scale_signal_increases_when_queued() -> None:
    provisioner = FakeProvisioner(max_slots=1)
    controller = PoolController(
        provisioner=provisioner,
        capacity_override=1,
        estimated_session_seconds=10,
        scale_up_threshold=1,
    )
    spec = SessionSpec(
        image_id="sample-image",
        cpu=4,
        memory_hint_mb=4096,
        ttl_seconds=60,
        idle_timeout_seconds=30,
    )
    s1, s2 = uuid4(), uuid4()
    h1 = await controller.acquire(s1, spec)
    queued = asyncio.create_task(controller.acquire(s2, spec))
    await asyncio.sleep(0)
    snapshot = await controller.snapshot()
    assert snapshot.scale_decision.scale_up_by == 1
    await controller.release(s1, h1)
    await queued


@pytest.mark.asyncio
async def test_pool_end_to_end_queue_and_release(pool_app: FastAPI) -> None:
    repo_id = await seed_repo(pool_app)
    async with AsyncClient(transport=ASGITransport(app=pool_app), base_url="http://test") as client:
        first = await client.post(
            "/v1/sessions",
            json={"repo_id": str(repo_id), "prompt": "first", "model_policy": {}},
        )
        assert first.status_code == 200
        first_id = UUID(first.json()["id"])
        await wait_for_status(client, first_id, SessionStatus.RUNNING)

        second = await client.post(
            "/v1/sessions",
            json={"repo_id": str(repo_id), "prompt": "second", "model_policy": {}},
        )
        assert second.status_code == 200
        second_id = UUID(second.json()["id"])
        await wait_for_status(client, second_id, SessionStatus.PROVISIONING)

        await wait_for_event_type(pool_app, second_id, "provisioner_queued")

        await wait_for_status(client, first_id, SessionStatus.SUCCEEDED)
        await wait_for_status(client, second_id, SessionStatus.SUCCEEDED)

        pool_response = await client.get("/v1/pool")
        assert pool_response.status_code == 200
        pool_payload = pool_response.json()
        assert pool_payload["capacity"] == 1
        assert pool_payload["queue_depth"] == 0


async def _capture_queue(events: list[tuple[int, int]], eta_seconds: int, position: int) -> None:
    events.append((eta_seconds, position))
