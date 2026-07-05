from __future__ import annotations

import pytest

from services.scheduler.provisioner.base import (
    AcquisitionState,
    ChannelKind,
    ProvisionerSessionStatus,
    SessionSpec,
)
from services.scheduler.provisioner.fake import FakeProvisioner


@pytest.mark.asyncio
async def test_fake_provisioner_ready_and_release() -> None:
    provisioner = FakeProvisioner()
    outcome = await provisioner.acquire(
        SessionSpec(
            image_id="sample",
            cpu=4,
            memory_hint_mb=4096,
            ttl_seconds=60,
            idle_timeout_seconds=30,
        )
    )
    assert outcome.state == AcquisitionState.READY
    assert await provisioner.status(outcome.handle) == ProvisionerSessionStatus.READY
    capacity = await provisioner.capacity()
    assert capacity.free_slots == 0
    await provisioner.release(outcome.handle)
    assert await provisioner.status(outcome.handle) == ProvisionerSessionStatus.GONE


@pytest.mark.asyncio
async def test_fake_provisioner_queued_capacity_and_reconcile() -> None:
    provisioner = FakeProvisioner(queue_acquire=True, queued_eta_seconds=99)
    outcome = await provisioner.acquire(
        SessionSpec(
            image_id="sample",
            cpu=2,
            memory_hint_mb=2048,
            ttl_seconds=60,
            idle_timeout_seconds=30,
        )
    )
    assert outcome.state == AcquisitionState.QUEUED
    assert outcome.eta_seconds == 99
    handles = await provisioner.reconcile()
    assert len(handles) == 1
    channel = await provisioner.open_channel(outcome.handle, ChannelKind.ARTIFACTS)
    assert channel.read()
    channel.close()
