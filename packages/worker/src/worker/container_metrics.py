from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import Field
from shared.contracts import ContractModel
from shared.realtime.contracts import CloudEventRecord, ContainerMetricsPayload

from worker.events import (
    ContainerRequestContext,
    GpuMemoryCounters,
    build_container_metrics_payload,
)
from worker.network_egress import NetworkEgressCounterSample
from worker.tools import (
    NetworkIoCounters,
    ProcessIoCounters,
    aggregate_network_counters,
    network_io_delta,
    process_io_delta,
)

LOGGER = logging.getLogger(__name__)


class ContainerMetricsSink(Protocol):
    def publish_container_metrics(
        self,
        payload: ContainerMetricsPayload,
    ) -> CloudEventRecord | None: ...


class ContainerMetricsSource(Protocol):
    def sample(self, request: ContainerRequestContext) -> ContainerMetricsRawSample: ...


class ContainerMetricsSourceFactory(Protocol):
    def metrics_source_for_pid(self, pid: int) -> ContainerMetricsSource: ...


class ContainerMetricsCounterState(ContractModel):
    process_io: ProcessIoCounters = Field(default_factory=ProcessIoCounters)
    network_io: NetworkIoCounters = Field(default_factory=NetworkIoCounters)
    network_egress: NetworkEgressCounterSample | None = None


class ContainerMetricsRawSample(ContractModel):
    cpu_used_millicores: int = 0
    memory_rss_bytes: int = 0
    memory_vms_bytes: int = 0
    memory_swap_bytes: int = 0
    process_io: ProcessIoCounters = Field(default_factory=ProcessIoCounters)
    network_interfaces: list[NetworkIoCounters] = Field(default_factory=list)
    gpu_memory: GpuMemoryCounters = Field(default_factory=GpuMemoryCounters)

    @property
    def network_io(self) -> NetworkIoCounters:
        return aggregate_network_counters(self.network_interfaces)

    def counter_state(self) -> ContainerMetricsCounterState:
        return ContainerMetricsCounterState(
            process_io=self.process_io,
            network_io=self.network_io,
        )


class ContainerMetricsSampleResult(ContractModel):
    payload: ContainerMetricsPayload | None = None
    next_state: ContainerMetricsCounterState
    published: bool = False
    reason: str = ""
    network_egress_bytes: int = 0


class ContainerDiskUsageSource(Protocol):
    def used_bytes(self, container_id: str) -> int: ...


class ContainerNetworkEgressSource(Protocol):
    def sample(self, container_id: str) -> NetworkEgressCounterSample: ...


@dataclass(slots=True)
class WorkerContainerMetricsService:
    worker_id: str
    sink: ContainerMetricsSink
    source: ContainerMetricsSource | None = None
    # Reports the bytes a container's own layer occupies, so ephemeral disk is
    # billed on what was actually used rather than on an oversubscribed cap.
    disk_usage: ContainerDiskUsageSource | None = None
    network_egress: ContainerNetworkEgressSource | None = None

    def sample_and_publish(
        self,
        request: ContainerRequestContext,
        *,
        previous: ContainerMetricsCounterState | None,
        sample_interval_ms: int,
    ) -> ContainerMetricsSampleResult:
        if self.source is None:
            msg = "metrics source is not configured"
            raise RuntimeError(msg)
        return self.publish_sample(
            request,
            self.source.sample(request),
            previous=previous,
            sample_interval_ms=sample_interval_ms,
        )

    def _disk_used_bytes(self, container_id: str) -> int:
        if self.disk_usage is None:
            return 0
        try:
            return self.disk_usage.used_bytes(container_id)
        except Exception:
            # Metrics are reported best effort; a usage read must never take the
            # container down.
            LOGGER.debug("disk usage read failed for %s", container_id, exc_info=True)
            return 0

    def publish_sample(
        self,
        request: ContainerRequestContext,
        sample: ContainerMetricsRawSample,
        *,
        previous: ContainerMetricsCounterState | None,
        sample_interval_ms: int,
    ) -> ContainerMetricsSampleResult:
        next_state = sample.counter_state()
        egress_bytes = 0
        if self.network_egress is not None:
            try:
                egress = self.network_egress.sample(request.container_id)
                next_state.network_egress = egress
                prior = previous.network_egress if previous is not None else None
                if prior is not None and prior.policy_digest == egress.policy_digest:
                    egress_bytes = max(0, egress.total_bytes - prior.total_bytes)
            except Exception:
                LOGGER.warning(
                    "internet egress classification unavailable for %s; interval is unbilled",
                    request.container_id,
                    exc_info=True,
                )
        if previous is None:
            return ContainerMetricsSampleResult(
                next_state=next_state,
                published=False,
                reason="metrics counter state primed",
            )
        payload = container_metrics_payload_from_sample(
            worker_id=self.worker_id,
            request=request,
            sample=sample,
            previous=previous,
            sample_interval_ms=sample_interval_ms,
            disk_used_bytes=self._disk_used_bytes(request.container_id),
        )
        self.sink.publish_container_metrics(payload)
        return ContainerMetricsSampleResult(
            payload=payload,
            next_state=next_state,
            network_egress_bytes=egress_bytes,
            published=True,
            reason="container metrics published",
        )


@dataclass(slots=True)
class ProcessTreeContainerMetricsSource:
    root_pid: int
    proc_root: Path = Path("/proc")
    _previous_process_jiffies: int | None = None
    _previous_system_jiffies: int | None = None

    def sample(self, request: ContainerRequestContext) -> ContainerMetricsRawSample:
        pids = self._process_tree_pids()
        process_jiffies = sum(self._process_jiffies(pid) for pid in pids)
        system_jiffies = self._system_jiffies()
        cpu_used_millicores = self._cpu_millicores(process_jiffies, system_jiffies)
        memory_rss_bytes = 0
        memory_vms_bytes = 0
        memory_swap_bytes = 0
        process_io = ProcessIoCounters()
        for pid in pids:
            rss, vms, swap = self._process_memory(pid)
            memory_rss_bytes += rss
            memory_vms_bytes += vms
            memory_swap_bytes += swap
            process_io = _sum_process_io(process_io, self._process_io(pid))
        return ContainerMetricsRawSample(
            cpu_used_millicores=cpu_used_millicores,
            memory_rss_bytes=memory_rss_bytes,
            memory_vms_bytes=memory_vms_bytes,
            memory_swap_bytes=memory_swap_bytes,
            process_io=process_io,
            network_interfaces=self._network_interfaces(),
        )

    def _process_tree_pids(self) -> list[int]:
        by_parent: dict[int, list[int]] = {}
        seen: set[int] = set()
        for entry in self.proc_root.iterdir() if self.proc_root.exists() else ():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            stat = self._stat_fields(pid)
            if stat is None:
                continue
            parent_pid = _int_at(stat, 1)
            by_parent.setdefault(parent_pid, []).append(pid)
        queue = [self.root_pid]
        pids: list[int] = []
        while queue:
            pid = queue.pop(0)
            if pid in seen:
                continue
            seen.add(pid)
            if (self.proc_root / str(pid)).exists():
                pids.append(pid)
            queue.extend(by_parent.get(pid, []))
        return pids

    def _stat_fields(self, pid: int) -> list[str] | None:
        path = self.proc_root / str(pid) / "stat"
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None
        _, separator, rest = raw.rpartition(")")
        if not separator:
            return None
        return rest.strip().split()

    def _process_jiffies(self, pid: int) -> int:
        stat = self._stat_fields(pid)
        if stat is None:
            return 0
        return _int_at(stat, 11) + _int_at(stat, 12)

    def _system_jiffies(self) -> int:
        try:
            first_line = (self.proc_root / "stat").read_text(encoding="utf-8").splitlines()[0]
        except (OSError, IndexError):
            return 0
        parts = first_line.split()
        if not parts or parts[0] != "cpu":
            return 0
        return sum(int(value) for value in parts[1:] if value.isdigit())

    def _cpu_millicores(self, process_jiffies: int, system_jiffies: int) -> int:
        previous_process = self._previous_process_jiffies
        previous_system = self._previous_system_jiffies
        self._previous_process_jiffies = process_jiffies
        self._previous_system_jiffies = system_jiffies
        if previous_process is None or previous_system is None:
            return 0
        process_delta = process_jiffies - previous_process
        system_delta = system_jiffies - previous_system
        if process_delta <= 0 or system_delta <= 0:
            return 0
        cpu_count = os.cpu_count() or 1
        return max(int(process_delta / system_delta * cpu_count * 1000), 0)

    def _process_memory(self, pid: int) -> tuple[int, int, int]:
        page_size = os.sysconf("SC_PAGE_SIZE")
        statm = self.proc_root / str(pid) / "statm"
        vms = 0
        rss = 0
        try:
            fields = statm.read_text(encoding="utf-8").split()
        except OSError:
            fields = []
        if len(fields) >= 2:
            vms = int(fields[0]) * page_size
            rss = int(fields[1]) * page_size
        return (rss, vms, self._process_swap(pid))

    def _process_swap(self, pid: int) -> int:
        status = self.proc_root / str(pid) / "status"
        try:
            lines = status.read_text(encoding="utf-8").splitlines()
        except OSError:
            return 0
        for line in lines:
            if line.startswith("VmSwap:"):
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    return int(parts[1]) * 1024
        return 0

    def _process_io(self, pid: int) -> ProcessIoCounters:
        values: dict[str, int] = {}
        try:
            lines = (self.proc_root / str(pid) / "io").read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for line in lines:
            key, separator, value = line.partition(":")
            if separator:
                values[key.strip()] = int(value.strip())
        return ProcessIoCounters(
            read_count=values.get("syscr", 0),
            write_count=values.get("syscw", 0),
            read_bytes=values.get("rchar", 0),
            write_bytes=values.get("wchar", 0),
            disk_read_bytes=values.get("read_bytes", 0),
            disk_write_bytes=values.get("write_bytes", 0),
        )

    def _network_interfaces(self) -> list[NetworkIoCounters]:
        net_dev = self.proc_root / str(self.root_pid) / "net" / "dev"
        try:
            lines = net_dev.read_text(encoding="utf-8").splitlines()[2:]
        except OSError:
            return []
        interfaces: list[NetworkIoCounters] = []
        for line in lines:
            name, separator, values = line.partition(":")
            if not separator:
                continue
            interface_name = name.strip()
            if interface_name == "lo":
                continue
            parts = values.split()
            if len(parts) < 10:
                continue
            interfaces.append(
                NetworkIoCounters(
                    name=interface_name,
                    bytes_recv=int(parts[0]),
                    packets_recv=int(parts[1]),
                    bytes_sent=int(parts[8]),
                    packets_sent=int(parts[9]),
                )
            )
        return interfaces


@dataclass(slots=True)
class ProcessTreeContainerMetricsSourceFactory:
    proc_root: Path = Path("/proc")

    def metrics_source_for_pid(self, pid: int) -> ProcessTreeContainerMetricsSource:
        return ProcessTreeContainerMetricsSource(
            root_pid=pid,
            proc_root=self.proc_root,
        )


def container_metrics_payload_from_sample(
    *,
    worker_id: str,
    request: ContainerRequestContext,
    sample: ContainerMetricsRawSample,
    previous: ContainerMetricsCounterState,
    sample_interval_ms: int,
    disk_used_bytes: int = 0,
) -> ContainerMetricsPayload:
    return build_container_metrics_payload(
        worker_id=worker_id,
        request=request,
        sample_interval_ms=max(sample_interval_ms, 0),
        cpu_used_millicores=max(sample.cpu_used_millicores, 0),
        memory_rss_bytes=max(sample.memory_rss_bytes, 0),
        memory_vms_bytes=max(sample.memory_vms_bytes, 0),
        memory_swap_bytes=max(sample.memory_swap_bytes, 0),
        process_io=process_io_delta(sample.process_io, previous.process_io),
        network_io=network_io_delta(sample.network_io, previous.network_io),
        gpu_memory=sample.gpu_memory,
        disk_used_bytes=max(disk_used_bytes, 0),
    )


def _int_at(values: list[str], index: int) -> int:
    if index >= len(values):
        return 0
    try:
        return int(values[index])
    except ValueError:
        return 0


def _sum_process_io(left: ProcessIoCounters, right: ProcessIoCounters) -> ProcessIoCounters:
    return ProcessIoCounters(
        read_count=left.read_count + right.read_count,
        write_count=left.write_count + right.write_count,
        read_bytes=left.read_bytes + right.read_bytes,
        write_bytes=left.write_bytes + right.write_bytes,
        disk_read_bytes=left.disk_read_bytes + right.disk_read_bytes,
        disk_write_bytes=left.disk_write_bytes + right.disk_write_bytes,
    )
