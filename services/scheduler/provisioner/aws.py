"""Real AWS + SSH edges for the EC2+tart provisioner.

``AwsCliHostDriver`` shells out to the ``aws`` CLI (no boto3 dependency) and
``SshTartRuntime`` runs ``tart`` over SSH on the mac host. Both are thin,
side-effecting adapters validated against real infrastructure (M3 burst),
not in unit tests — all driver logic lives in ``Ec2TartProvisioner``.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from services.scheduler.provisioner.ec2_tart import (
    MAC_HOST_MIN_ALLOC_SECONDS,
    HostDriver,
    HostState,
    MacHostInfo,
    TartVmRuntime,
)


async def _run(argv: list[str], timeout_seconds: int = 300) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    rc = process.returncode if process.returncode is not None else -1
    return rc, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")


async def _run_ok(argv: list[str], timeout_seconds: int = 300) -> str:
    rc, stdout, stderr = await _run(argv, timeout_seconds)
    if rc != 0:
        raise RuntimeError(f"{argv[0]} failed (rc={rc}): {stderr.strip()}")
    return stdout


@dataclass(slots=True)
class AwsCliHostDriver(HostDriver):
    """Dedicated EC2 Mac host driver backed by the ``aws`` CLI."""

    region: str
    availability_zone: str
    instance_type: str = "mac2.metal"
    ami_id: str = ""
    key_name: str = ""
    tag_value: str = "raven"

    def _aws(self, *args: str) -> list[str]:
        return ["aws", "--region", self.region, "--output", "json", *args]

    async def list_hosts(self) -> list[MacHostInfo]:
        stdout = await _run_ok(
            self._aws(
                "ec2",
                "describe-hosts",
                "--filter",
                f"Name=tag:raven,Values={self.tag_value}",
                "Name=state,Values=pending,available",
            )
        )
        payload = json.loads(stdout)
        hosts: list[MacHostInfo] = []
        for entry in payload.get("Hosts", []):
            host_id = str(entry["HostId"])
            allocated_at = datetime.fromisoformat(str(entry["AllocationTime"])).astimezone(UTC)
            address = await self._instance_address(host_id)
            state = (
                HostState.READY
                if entry.get("State") == "available" and address
                else (HostState.PROVISIONING)
            )
            hosts.append(
                MacHostInfo(
                    host_id=host_id,
                    state=state,
                    address=address,
                    allocated_at=allocated_at,
                    min_release_at=allocated_at + timedelta(seconds=MAC_HOST_MIN_ALLOC_SECONDS),
                )
            )
        return hosts

    async def provision_host(self) -> MacHostInfo:
        stdout = await _run_ok(
            self._aws(
                "ec2",
                "allocate-hosts",
                "--availability-zone",
                self.availability_zone,
                "--instance-type",
                self.instance_type,
                "--quantity",
                "1",
                "--tag-specifications",
                f"ResourceType=dedicated-host,Tags=[{{Key=raven,Value={self.tag_value}}}]",
            )
        )
        host_id = str(json.loads(stdout)["HostIds"][0])
        await _run_ok(
            self._aws(
                "ec2",
                "run-instances",
                "--image-id",
                self.ami_id,
                "--instance-type",
                self.instance_type,
                "--key-name",
                self.key_name,
                "--placement",
                f"HostId={host_id}",
                "--tag-specifications",
                f"ResourceType=instance,Tags=[{{Key=raven,Value={self.tag_value}}}]",
            )
        )
        now = datetime.now(UTC)
        return MacHostInfo(
            host_id=host_id,
            state=HostState.PROVISIONING,
            address="",
            allocated_at=now,
            min_release_at=now + timedelta(seconds=MAC_HOST_MIN_ALLOC_SECONDS),
        )

    async def release_host(self, host_id: str) -> None:
        instance_id = await self._instance_id(host_id)
        if instance_id:
            await _run_ok(self._aws("ec2", "terminate-instances", "--instance-ids", instance_id))
        # NOTE: release fails until the 24h minimum allocation elapses and the
        # instance fully terminates; the provisioner only calls this for idle
        # hosts past min_release_at.
        await _run_ok(self._aws("ec2", "release-hosts", "--host-ids", host_id))

    async def _instance_id(self, host_id: str) -> str:
        stdout = await _run_ok(
            self._aws(
                "ec2",
                "describe-instances",
                "--filters",
                f"Name=placement.host-id,Values={host_id}",
                "Name=instance-state-name,Values=pending,running",
            )
        )
        for reservation in json.loads(stdout).get("Reservations", []):
            for instance in reservation.get("Instances", []):
                return str(instance["InstanceId"])
        return ""

    async def _instance_address(self, host_id: str) -> str:
        stdout = await _run_ok(
            self._aws(
                "ec2",
                "describe-instances",
                "--filters",
                f"Name=placement.host-id,Values={host_id}",
                "Name=instance-state-name,Values=running",
            )
        )
        for reservation in json.loads(stdout).get("Reservations", []):
            for instance in reservation.get("Instances", []):
                address = instance.get("PublicIpAddress") or instance.get("PrivateIpAddress")
                if address:
                    return str(address)
        return ""


@dataclass(slots=True)
class SshTartRuntime(TartVmRuntime):
    """Runs ``tart`` on the mac host over SSH; the worker daemon runs in the VM.

    Worker ports are derived per-VM and reached through the host address —
    the golden image's launchd job forwards the port from the VM (see
    ``scripts/golden-image/``).
    """

    ssh_user: str = "ec2-user"
    ssh_key_path: str = ""
    worker_base_port: int = 8760
    ssh_timeout_seconds: int = 600

    def _ssh(self, host: MacHostInfo, remote_command: str) -> list[str]:
        argv = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes"]
        if self.ssh_key_path:
            argv += ["-i", self.ssh_key_path]
        argv += [f"{self.ssh_user}@{host.address}", remote_command]
        return argv

    async def _ssh_ok(self, host: MacHostInfo, remote_command: str) -> str:
        return await _run_ok(self._ssh(host, remote_command), self.ssh_timeout_seconds)

    def _worker_port(self, vm_name: str) -> int:
        # Stable per-VM port so reconcile can recompute endpoints.
        return self.worker_base_port + (zlib.crc32(vm_name.encode("utf-8")) % 100)

    async def clone_vm(self, host: MacHostInfo, source_image: str, vm_name: str) -> None:
        await self._ssh_ok(host, f"tart clone {shlex.quote(source_image)} {shlex.quote(vm_name)}")

    async def start_vm(self, host: MacHostInfo, vm_name: str) -> None:
        await self._ssh_ok(
            host, f"nohup tart run --no-graphics {shlex.quote(vm_name)} >/dev/null 2>&1 &"
        )

    async def start_worker(self, host: MacHostInfo, vm_name: str, token: str) -> str:
        port = self._worker_port(vm_name)
        vm_ip = (await self._ssh_ok(host, f"tart ip {shlex.quote(vm_name)}")).strip()
        script = (
            f"FURNACE_WORKER_TOKEN={shlex.quote(token)} "
            f"FURNACE_WORKER_HOST=0.0.0.0 FURNACE_WORKER_PORT=8787 "
            "FURNACE_WORKER_MODE=real "
            "nohup python3 -m apps.worker >/tmp/raven-worker.log 2>&1 &"
        )
        await self._ssh_ok(
            host,
            f"ssh -o StrictHostKeyChecking=accept-new admin@{shlex.quote(vm_ip)} "
            f"{shlex.quote(script)}",
        )
        await self._ssh_ok(
            host,
            f"nohup socat TCP-LISTEN:{port},fork,reuseaddr TCP:{vm_ip}:8787 " f">/dev/null 2>&1 &",
        )
        return f"http://{host.address}:{port}"

    async def stop_vm(self, host: MacHostInfo, vm_name: str) -> None:
        rc, _stdout, _stderr = await _run(
            self._ssh(host, f"tart stop {shlex.quote(vm_name)}"), self.ssh_timeout_seconds
        )
        _ = rc  # already-stopped VMs are fine; delete is the authoritative cleanup

    async def delete_vm(self, host: MacHostInfo, vm_name: str) -> None:
        await self._ssh_ok(host, f"tart delete {shlex.quote(vm_name)}")

    async def list_vms(self, host: MacHostInfo) -> list[str]:
        stdout = await self._ssh_ok(host, "tart list --format json")
        entries = json.loads(stdout)
        return [str(entry["Name"]) for entry in entries if entry.get("Source") == "local"]

    async def worker_endpoint(self, host: MacHostInfo, vm_name: str) -> str:
        return f"http://{host.address}:{self._worker_port(vm_name)}"
