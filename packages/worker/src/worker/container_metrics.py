from __future__ import annotations

import logging
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
from worker.network_egress import (
    NetworkEgressCounterSample,
    NetworkEgressPolicyUnavailableError,
)
from worker.runtime_config import absolute_container_accounting_cgroup_path
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
    def metrics_source_for_container(self, container_id: str) -> ContainerMetricsSource: ...


class ContainerMetricsCounterState(ContractModel):
    cpu_usage_usec: int | None = None
    memory_rss_bytes: int = 0
    process_io: ProcessIoCounters = Field(default_factory=ProcessIoCounters)
    network_io: NetworkIoCounters = Field(default_factory=NetworkIoCounters)
    network_egress: NetworkEgressCounterSample | None = None


class ContainerMetricsRawSample(ContractModel):
    measurement_complete: bool = False
    cpu_usage_usec: int | None = None
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
            cpu_usage_usec=self.cpu_usage_usec,
            memory_rss_bytes=self.memory_rss_bytes,
            process_io=self.process_io,
            network_io=self.network_io,
        )


class ContainerMetricsSampleResult(ContractModel):
    measurement_complete: bool = False
    cpu_used_core_seconds: float = 0
    memory_rss_byte_seconds: float = 0
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
            except NetworkEgressPolicyUnavailableError:
                if previous is None or previous.network_egress is not None:
                    LOGGER.warning(
                        "internet egress evidence unavailable for %s; interval is unbilled",
                        request.container_id,
                    )
            except Exception:
                LOGGER.warning(
                    "internet egress classification unavailable for %s; interval is unbilled",
                    request.container_id,
                    exc_info=True,
                )
        if previous is None:
            return ContainerMetricsSampleResult(
                measurement_complete=sample.measurement_complete,
                next_state=next_state,
                published=False,
                reason="metrics counter state primed",
            )
        cpu_seconds = 0.0
        complete = sample.measurement_complete
        if sample.cpu_usage_usec is not None and previous.cpu_usage_usec is not None:
            if sample.cpu_usage_usec < previous.cpu_usage_usec:
                raise RuntimeError("container CPU accounting counter moved backwards")
            cpu_seconds = (sample.cpu_usage_usec - previous.cpu_usage_usec) / 1_000_000
            sample = sample.model_copy(
                update={
                    "cpu_used_millicores": round(
                        cpu_seconds * 1_000_000 / max(sample_interval_ms, 1)
                    )
                }
            )
        else:
            complete = False
        payload = container_metrics_payload_from_sample(
            worker_id=self.worker_id,
            request=request,
            sample=sample,
            previous=previous,
            sample_interval_ms=sample_interval_ms,
            disk_used_bytes=self._disk_used_bytes(request.container_id),
        )
        published = False
        try:
            self.sink.publish_container_metrics(payload)
            published = True
        except Exception:
            LOGGER.warning(
                "container metrics publication failed",
                exc_info=True,
                extra={"container_id": request.container_id},
            )
        return ContainerMetricsSampleResult(
            payload=payload,
            measurement_complete=complete,
            cpu_used_core_seconds=cpu_seconds,
            memory_rss_byte_seconds=previous.memory_rss_bytes * sample_interval_ms / 1_000,
            next_state=next_state,
            network_egress_bytes=egress_bytes,
            published=published,
            reason="container metrics published"
            if published
            else "container metrics publication failed",
        )


@dataclass(slots=True)
class CgroupContainerMetricsSource:
    directory: Path
    proc_root: Path = Path("/proc")

    def sample(self, request: ContainerRequestContext) -> ContainerMetricsRawSample:
        cpu = self._counters("cpu.stat")
        memory = self._counters("memory.stat")
        if "usage_usec" not in cpu or not {"anon", "file_mapped"}.issubset(memory):
            raise RuntimeError("container compute accounting counters are unavailable")
        process_io = ProcessIoCounters()
        pids: list[int] = []
        for file in self.directory.rglob("cgroup.procs"):
            pids.extend(int(value) for value in file.read_text().split())
        for pid in set(pids):
            try:
                values = {
                    key.rstrip(":"): value
                    for key, value in (
                        line.split()
                        for line in (self.proc_root / str(pid) / "io").read_text().splitlines()
                    )
                }
            except OSError:
                continue
            process_io = _sum_process_io(
                process_io,
                ProcessIoCounters(
                    read_count=int(values.get("syscr", "0")),
                    write_count=int(values.get("syscw", "0")),
                    read_bytes=int(values.get("rchar", "0")),
                    write_bytes=int(values.get("wchar", "0")),
                    disk_read_bytes=int(values.get("read_bytes", "0")),
                    disk_write_bytes=int(values.get("write_bytes", "0")),
                ),
            )
        return ContainerMetricsRawSample(
            measurement_complete=True,
            cpu_usage_usec=cpu["usage_usec"],
            memory_rss_bytes=memory["anon"] + memory["file_mapped"],
            memory_swap_bytes=int((self.directory / "memory.swap.current").read_text()),
            process_io=process_io,
            network_interfaces=self._network_interfaces(pids),
        )

    def _counters(self, name: str) -> dict[str, int]:
        return {
            key: int(value)
            for key, value in (
                line.split() for line in (self.directory / name).read_text().splitlines()
            )
        }

    def _network_interfaces(self, pids: list[int]) -> list[NetworkIoCounters]:
        if not pids:
            return []
        try:
            lines = (self.proc_root / str(pids[0]) / "net" / "dev").read_text().splitlines()[2:]
        except OSError:
            return []
        interfaces: list[NetworkIoCounters] = []
        for line in lines:
            name, separator, values = line.partition(":")
            if not separator or name.strip() == "lo":
                continue
            parts = values.split()
            if len(parts) < 10:
                continue
            interfaces.append(
                NetworkIoCounters(
                    name=name.strip(),
                    bytes_recv=int(parts[0]),
                    packets_recv=int(parts[1]),
                    bytes_sent=int(parts[8]),
                    packets_sent=int(parts[9]),
                )
            )
        return interfaces


@dataclass(slots=True)
class CgroupContainerMetricsSourceFactory:
    def metrics_source_for_container(self, container_id: str) -> CgroupContainerMetricsSource:
        directory = absolute_container_accounting_cgroup_path(container_id)
        if not directory:
            raise RuntimeError("container accounting cgroup is unavailable")
        return CgroupContainerMetricsSource(directory=Path(directory))


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


def _sum_process_io(left: ProcessIoCounters, right: ProcessIoCounters) -> ProcessIoCounters:
    return ProcessIoCounters(
        read_count=left.read_count + right.read_count,
        write_count=left.write_count + right.write_count,
        read_bytes=left.read_bytes + right.read_bytes,
        write_bytes=left.write_bytes + right.write_bytes,
        disk_read_bytes=left.disk_read_bytes + right.disk_read_bytes,
        disk_write_bytes=left.disk_write_bytes + right.disk_write_bytes,
    )
