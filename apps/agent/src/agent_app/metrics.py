from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from subprocess import CompletedProcess
from typing import Protocol

from agent.service_manager import (
    bytes_to_mib,
    cgroup_memory_override,
    parse_cgroup_uint_text,
)
from gateway.http import AgentMetricSnapshot
from shared.contracts import ContractModel
from worker.gpu import NVIDIA_VISIBLE_DEVICES_ALL, available_gpu_indices

CGROUP_V2_MEMORY_MAX = Path("/sys/fs/cgroup/memory.max")
CGROUP_V2_MEMORY_CURRENT = Path("/sys/fs/cgroup/memory.current")
CGROUP_V1_MEMORY_LIMIT = Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")
CGROUP_V1_MEMORY_USAGE = Path("/sys/fs/cgroup/memory/memory.usage_in_bytes")


class AgentGpuProvider(Protocol):
    def available_devices(self) -> list[int]: ...


class AgentMemorySample(ContractModel):
    used_mb: int = 0
    total_mb: int = 0
    utilization_pct: float = 0.0


class AgentDiskSample(ContractModel):
    used_mb: int = 0
    total_mb: int = 0
    usage_pct: float = 0.0
    path: str = "/"


class AgentNetworkSample(ContractModel):
    recv_bytes: int = 0
    sent_bytes: int = 0
    recv_packets: int = 0
    sent_packets: int = 0


@dataclass(slots=True)
class NvidiaSmiGpuProvider:
    visible_devices: str = NVIDIA_VISIBLE_DEVICES_ALL
    command_runner: Callable[..., CompletedProcess[str]] = subprocess.run

    def available_devices(self) -> list[int]:
        if shutil.which("nvidia-smi") is None:
            return []
        try:
            result = self.command_runner(
                [
                    "nvidia-smi",
                    "--query-gpu=pci.domain,pci.bus_id,index,uuid",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return []
        if not isinstance(result, CompletedProcess) or result.returncode != 0:
            return []
        return available_gpu_indices(
            result.stdout,
            visible_devices=self.visible_devices or NVIDIA_VISIBLE_DEVICES_ALL,
        )


@dataclass(slots=True)
class AgentMetricSampler:
    state_dir: Path
    gpu_provider: AgentGpuProvider = field(default_factory=NvidiaSmiGpuProvider)

    def snapshot(self, *, worker_count: int) -> AgentMetricSnapshot:
        memory = agent_memory_sample()
        disk = agent_disk_sample(self.state_dir)
        network = agent_network_sample()
        return AgentMetricSnapshot(
            timestamp_unix_nano=time.time_ns(),
            cpu_utilization_pct=agent_cpu_utilization_pct(),
            memory_used_mb=memory.used_mb,
            memory_total_mb=memory.total_mb,
            memory_utilization_pct=memory.utilization_pct,
            disk_used_mb=disk.used_mb,
            disk_total_mb=disk.total_mb,
            disk_usage_pct=disk.usage_pct,
            disk_path=disk.path,
            network_recv_bytes=network.recv_bytes,
            network_sent_bytes=network.sent_bytes,
            network_recv_packets=network.recv_packets,
            network_sent_packets=network.sent_packets,
            worker_count=worker_count,
            free_gpu_count=len(self.gpu_provider.available_devices()),
        )


def agent_metric_snapshot(
    state_dir: Path,
    worker_count: int,
    *,
    gpu_provider: AgentGpuProvider | None = None,
) -> AgentMetricSnapshot:
    return AgentMetricSampler(
        state_dir,
        gpu_provider=gpu_provider or NvidiaSmiGpuProvider(),
    ).snapshot(worker_count=worker_count)


def agent_cpu_utilization_pct(
    *,
    cpu_count: int | None = None,
    load_average: float | None = None,
) -> float:
    count = max(cpu_count or os.cpu_count() or 1, 1)
    if load_average is None:
        try:
            load_average = os.getloadavg()[0]
        except OSError:
            return 0.0
    return round(max(min(load_average / count * 100, 100.0), 0.0), 2)


def agent_memory_sample(
    *,
    meminfo_text: str | None = None,
    cgroup_files: dict[Path, str] | None = None,
) -> AgentMemorySample:
    host_used_bytes, host_total_bytes = _host_memory_bytes(meminfo_text=meminfo_text)
    cgroup_used_bytes, cgroup_limit_bytes = _cgroup_memory_bytes(cgroup_files=cgroup_files)
    used_bytes, total_bytes = cgroup_memory_override(
        host_used_bytes=host_used_bytes,
        host_total_bytes=host_total_bytes,
        cgroup_used_bytes=cgroup_used_bytes,
        cgroup_limit_bytes=cgroup_limit_bytes,
    )
    used_mb = bytes_to_mib(used_bytes)
    total_mb = bytes_to_mib(total_bytes)
    utilization_pct = (used_bytes / total_bytes * 100) if total_bytes else 0.0
    return AgentMemorySample(
        used_mb=used_mb,
        total_mb=total_mb,
        utilization_pct=round(utilization_pct, 2),
    )


def agent_disk_sample(state_dir: Path) -> AgentDiskSample:
    path = Path("/")
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        state_dir.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(state_dir)
        path = state_dir
    used_bytes = max(usage.total - usage.free, 0)
    usage_pct = (used_bytes / usage.total * 100) if usage.total else 0.0
    return AgentDiskSample(
        used_mb=bytes_to_mib(used_bytes),
        total_mb=bytes_to_mib(usage.total),
        usage_pct=round(usage_pct, 2),
        path=str(path),
    )


def agent_network_sample(*, netdev_text: str | None = None) -> AgentNetworkSample:
    text = netdev_text
    if text is None:
        try:
            text = Path("/proc/net/dev").read_text()
        except OSError:
            return AgentNetworkSample()
    recv_bytes = 0
    sent_bytes = 0
    recv_packets = 0
    sent_packets = 0
    for line in text.splitlines():
        if ":" not in line:
            continue
        interface, raw_values = line.split(":", 1)
        if interface.strip() == "lo":
            continue
        values = raw_values.split()
        if len(values) < 10:
            continue
        try:
            recv_bytes += int(values[0])
            recv_packets += int(values[1])
            sent_bytes += int(values[8])
            sent_packets += int(values[9])
        except ValueError:
            continue
    return AgentNetworkSample(
        recv_bytes=max(recv_bytes, 0),
        sent_bytes=max(sent_bytes, 0),
        recv_packets=max(recv_packets, 0),
        sent_packets=max(sent_packets, 0),
    )


def physical_memory_mb() -> int:
    return agent_memory_sample(cgroup_files={}).total_mb or _sysconf_memory_mb()


def _host_memory_bytes(*, meminfo_text: str | None) -> tuple[int, int]:
    text = meminfo_text
    if text is None:
        try:
            text = Path("/proc/meminfo").read_text()
        except OSError:
            total_mb = _sysconf_memory_mb()
            return (0, total_mb * 1024 * 1024)
    total_kb = 0
    available_kb = 0
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 2:
            continue
        if fields[0] == "MemTotal:":
            total_kb = int(fields[1])
        elif fields[0] == "MemAvailable:":
            available_kb = int(fields[1])
    total_bytes = total_kb * 1024
    used_bytes = max(total_bytes - available_kb * 1024, 0)
    return (used_bytes, total_bytes)


def _cgroup_memory_bytes(
    *,
    cgroup_files: dict[Path, str] | None = None,
) -> tuple[int | None, int | None]:
    limit = _read_cgroup_uint(CGROUP_V2_MEMORY_MAX, cgroup_files)
    used = _read_cgroup_uint(CGROUP_V2_MEMORY_CURRENT, cgroup_files)
    if limit is not None and used is not None:
        return (used, limit)
    limit = _read_cgroup_uint(CGROUP_V1_MEMORY_LIMIT, cgroup_files)
    used = _read_cgroup_uint(CGROUP_V1_MEMORY_USAGE, cgroup_files)
    return (used, limit)


def _read_cgroup_uint(path: Path, cgroup_files: dict[Path, str] | None) -> int | None:
    if cgroup_files is not None:
        value = cgroup_files.get(path)
        return parse_cgroup_uint_text(value or "")
    try:
        return parse_cgroup_uint_text(path.read_text())
    except OSError:
        return None


def _sysconf_memory_mb() -> int:
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, ValueError):
        return 1024
    if not isinstance(pages, int) or not isinstance(page_size, int):
        return 1024
    return max((pages * page_size) // (1024 * 1024), 1)
