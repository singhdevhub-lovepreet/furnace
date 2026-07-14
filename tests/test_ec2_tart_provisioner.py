from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from apps.worker.client import WorkerToolClient
from apps.worker.daemon import create_worker_app
from services.scheduler.provisioner.base import (
    AcquisitionState,
    ChannelKind,
    ProvisionerSessionStatus,
    SessionHandle,
    SessionSpec,
)
from services.scheduler.provisioner.ec2_tart import (
    Ec2TartProvisioner,
    HostDriver,
    HostState,
    MacHostInfo,
    TartVmRuntime,
    vm_name_for,
)


def make_spec() -> SessionSpec:
    return SessionSpec(
        image_id="raven-golden",
        cpu=4,
        memory_hint_mb=8192,
        ttl_seconds=1800,
        idle_timeout_seconds=300,
    )


def make_host(host_id: str = "h-1", *, releasable: bool = False) -> MacHostInfo:
    allocated = datetime.now(UTC) - timedelta(hours=25 if releasable else 1)
    return MacHostInfo(
        host_id=host_id,
        state=HostState.READY,
        address=f"10.0.0.{host_id[-1]}",
        allocated_at=allocated,
        min_release_at=allocated + timedelta(hours=24),
    )


@dataclass(slots=True)
class FakeHostDriver(HostDriver):
    hosts: list[MacHostInfo] = field(default_factory=list)
    provision_calls: int = 0
    released: list[str] = field(default_factory=list)

    async def list_hosts(self) -> list[MacHostInfo]:
        return [host for host in self.hosts if host.host_id not in self.released]

    async def provision_host(self) -> MacHostInfo:
        self.provision_calls += 1
        allocated = datetime.now(UTC)
        host = MacHostInfo(
            host_id=f"h-new-{self.provision_calls}",
            state=HostState.PROVISIONING,
            address="",
            allocated_at=allocated,
            min_release_at=allocated + timedelta(hours=24),
        )
        self.hosts.append(host)
        return host

    async def release_host(self, host_id: str) -> None:
        self.released.append(host_id)


@dataclass(slots=True)
class FakeTartRuntime(TartVmRuntime):
    """Fake tart: tracks per-host VMs and backs each with a real worker app."""

    workspace_root: Path
    calls: list[tuple[str, str, str]] = field(default_factory=list)
    vms: dict[str, list[str]] = field(default_factory=dict)
    tokens: dict[str, str] = field(default_factory=dict)

    async def clone_vm(self, host: MacHostInfo, source_image: str, vm_name: str) -> None:
        self.calls.append(("clone", host.host_id, vm_name))
        assert source_image == "raven-golden"
        self.vms.setdefault(host.host_id, []).append(vm_name)

    async def start_vm(self, host: MacHostInfo, vm_name: str) -> None:
        self.calls.append(("start", host.host_id, vm_name))

    async def start_worker(self, host: MacHostInfo, vm_name: str, token: str) -> str:
        self.calls.append(("start_worker", host.host_id, vm_name))
        self.tokens[vm_name] = token
        return f"http://{vm_name}.{host.host_id}.vm:8787"

    async def stop_vm(self, host: MacHostInfo, vm_name: str) -> None:
        self.calls.append(("stop", host.host_id, vm_name))

    async def delete_vm(self, host: MacHostInfo, vm_name: str) -> None:
        self.calls.append(("delete", host.host_id, vm_name))
        self.vms.get(host.host_id, []).remove(vm_name)

    async def list_vms(self, host: MacHostInfo) -> list[str]:
        return list(self.vms.get(host.host_id, []))

    async def worker_endpoint(self, host: MacHostInfo, vm_name: str) -> str:
        return f"http://{vm_name}.{host.host_id}.vm:8787"


def make_provisioner(
    tmp_path: Path,
    hosts: list[MacHostInfo],
    *,
    vms_per_host: int = 2,
) -> tuple[Ec2TartProvisioner, FakeHostDriver, FakeTartRuntime]:
    driver = FakeHostDriver(hosts=hosts)
    runtime = FakeTartRuntime(workspace_root=tmp_path)

    def client_factory(base_url: str, token: str) -> WorkerToolClient:
        vm_name = base_url.removeprefix("http://").split(".", 1)[0]
        workspace = tmp_path / vm_name
        app = create_worker_app(mode="fake", workspace_root=workspace, auth_token=token)
        transport = httpx.ASGITransport(app=app)
        http_client = httpx.AsyncClient(transport=transport, base_url=base_url)
        return WorkerToolClient(base_url, token=token, http_client=http_client)

    provisioner = Ec2TartProvisioner(
        host_driver=driver,
        vm_runtime=runtime,
        golden_vm="raven-golden",
        vms_per_host=vms_per_host,
        client_factory=client_factory,
        health_retries=2,
        health_retry_delay_seconds=0.01,
    )
    return provisioner, driver, runtime


async def test_acquire_clones_vm_boots_worker_and_wires_endpoint(tmp_path: Path) -> None:
    provisioner, _driver, runtime = make_provisioner(tmp_path, [make_host()])
    outcome = await provisioner.acquire(make_spec())
    assert outcome.state is AcquisitionState.READY
    vm_name = vm_name_for(outcome.handle.id)
    assert outcome.handle.provider == "ec2_tart"
    assert outcome.handle.tool_endpoint == f"http://{vm_name}.h-1.vm:8787"
    assert [call[0] for call in runtime.calls] == ["clone", "start", "start_worker"]
    assert runtime.vms["h-1"] == [vm_name]
    assert await provisioner.status(outcome.handle) is ProvisionerSessionStatus.READY


async def test_exec_and_files_delegate_to_worker(tmp_path: Path) -> None:
    provisioner, _driver, _runtime = make_provisioner(tmp_path, [make_host()])
    outcome = await provisioner.acquire(make_spec())
    handle = outcome.handle

    await provisioner.put_file(handle, "assets/logo.png", b"\x89PNG\x00binary")
    assert await provisioner.get_file(handle, "assets/logo.png") == b"\x89PNG\x00binary"

    rc, stdout, stderr = await provisioner.exec(handle, "pwd", [], {}, 30)
    assert rc == 0
    assert stdout.strip().endswith(vm_name_for(handle.id))
    assert stderr == ""

    rc, _stdout, _stderr = await provisioner.exec(handle, "false", [], {}, 30)
    assert rc != 0


async def test_put_file_rejects_workspace_escape(tmp_path: Path) -> None:
    provisioner, _driver, _runtime = make_provisioner(tmp_path, [make_host()])
    outcome = await provisioner.acquire(make_spec())
    with pytest.raises(RuntimeError, match="path escapes workspace"):
        await provisioner.put_file(outcome.handle, "../outside.txt", b"nope")


async def test_release_deletes_vm_and_frees_slot(tmp_path: Path) -> None:
    provisioner, _driver, runtime = make_provisioner(tmp_path, [make_host()], vms_per_host=1)
    outcome = await provisioner.acquire(make_spec())
    report = await provisioner.capacity()
    assert (report.free_slots, report.total_slots) == (0, 1)

    await provisioner.release(outcome.handle)
    assert [call[0] for call in runtime.calls[-2:]] == ["stop", "delete"]
    assert runtime.vms["h-1"] == []
    assert await provisioner.status(outcome.handle) is ProvisionerSessionStatus.GONE
    report = await provisioner.capacity()
    assert (report.free_slots, report.total_slots) == (1, 1)


async def test_acquire_queues_and_kicks_provisioning_when_full(tmp_path: Path) -> None:
    provisioner, driver, _runtime = make_provisioner(tmp_path, [make_host()], vms_per_host=1)
    first = await provisioner.acquire(make_spec())
    assert first.state is AcquisitionState.READY

    second = await provisioner.acquire(make_spec())
    assert second.state is AcquisitionState.QUEUED
    assert second.eta_seconds == provisioner.est_new_slot_seconds
    assert await provisioner.status(second.handle) is ProvisionerSessionStatus.QUEUED
    for task in list(provisioner._provision_tasks):
        await task
    assert driver.provision_calls == 1

    report = await provisioner.capacity()
    assert report.provisioning_hosts == 1
    assert report.warm_hosts == 1


async def test_warm_host_accounting_across_hosts(tmp_path: Path) -> None:
    hosts = [make_host("h-1"), make_host("h-2")]
    provisioner, _driver, runtime = make_provisioner(tmp_path, hosts, vms_per_host=2)
    report = await provisioner.capacity()
    assert (report.total_slots, report.free_slots, report.warm_hosts) == (4, 4, 2)

    outcomes = [await provisioner.acquire(make_spec()) for _ in range(3)]
    assert all(outcome.state is AcquisitionState.READY for outcome in outcomes)
    # First host fills up (2 VMs) before the second is used.
    assert len(runtime.vms["h-1"]) == 2
    assert len(runtime.vms["h-2"]) == 1
    report = await provisioner.capacity()
    assert (report.total_slots, report.free_slots) == (4, 1)


async def test_release_idle_host_past_min_allocation(tmp_path: Path) -> None:
    releasable = make_host("h-1", releasable=True)
    provisioner, driver, _runtime = make_provisioner(tmp_path, [releasable])
    outcome = await provisioner.acquire(make_spec())
    await provisioner.release(outcome.handle)
    assert driver.released == ["h-1"]


async def test_fresh_host_not_released_before_min_allocation(tmp_path: Path) -> None:
    provisioner, driver, _runtime = make_provisioner(tmp_path, [make_host("h-1")])
    outcome = await provisioner.acquire(make_spec())
    await provisioner.release(outcome.handle)
    assert driver.released == []


async def test_reconcile_rediscovers_vms_from_hosts(tmp_path: Path) -> None:
    host = make_host("h-1")
    provisioner, _driver, runtime = make_provisioner(tmp_path, [host])
    orphan_id = uuid4()
    runtime.vms["h-1"] = [vm_name_for(orphan_id), "not-a-raven-vm"]

    handles = await provisioner.reconcile()
    assert [handle.id for handle in handles] == [orphan_id]
    assert handles[0].tool_endpoint == f"http://{vm_name_for(orphan_id)}.h-1.vm:8787"

    report = await provisioner.capacity()
    assert report.free_slots == 1  # the orphan occupies a slot

    await provisioner.release(handles[0])
    assert runtime.vms["h-1"] == ["not-a-raven-vm"]


async def test_open_channel_exposes_tool_rpc_endpoint_only(tmp_path: Path) -> None:
    provisioner, _driver, _runtime = make_provisioner(tmp_path, [make_host()])
    outcome = await provisioner.acquire(make_spec())
    channel = await provisioner.open_channel(outcome.handle, ChannelKind.TOOL_RPC)
    assert channel.read().decode() == outcome.handle.tool_endpoint
    with pytest.raises(NotImplementedError):
        await provisioner.open_channel(outcome.handle, ChannelKind.VNC)


async def test_failed_worker_boot_cleans_up_vm(tmp_path: Path) -> None:
    provisioner, _driver, runtime = make_provisioner(tmp_path, [make_host()])

    def broken_factory(base_url: str, token: str) -> WorkerToolClient:
        _ = base_url
        return WorkerToolClient("http://127.0.0.1:9", token=token, timeout_seconds=0.2)

    provisioner.client_factory = broken_factory
    with pytest.raises(RuntimeError, match="never became healthy"):
        await provisioner.acquire(make_spec())
    assert runtime.vms["h-1"] == []
    report = await provisioner.capacity()
    assert report.free_slots == report.total_slots


async def test_status_of_unknown_handle_is_gone(tmp_path: Path) -> None:
    provisioner, _driver, _runtime = make_provisioner(tmp_path, [make_host()])
    unknown = SessionHandle(
        id=uuid4(),
        provider="ec2_tart",
        created_at=datetime.now(UTC),
        tool_endpoint="pending://ec2-tart",
    )
    assert await provisioner.status(unknown) is ProvisionerSessionStatus.GONE
