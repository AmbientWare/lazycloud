from __future__ import annotations

from shared.containers import ContainerRecord
from shared.placement import Placement
from shared.scheduling import SchedulerWorkerRequest
from shared.timestamps import to_utc, to_utc_or_none

from database.tables.orchestration import ContainerTable


def container_from_row(row: ContainerTable) -> ContainerRecord:
    return ContainerRecord.model_validate(
        {
            "id": row.id,
            "name": row.name,
            "image": row.image,
            "command": row.command,
            "workspace_id": row.workspace_id,
            "stub_id": row.stub_id,
            "app_id": row.app_id,
            "machine_id": row.machine_id,
            "worker_id": row.worker_id,
            "runtime_machine_id": row.runtime_machine_id,
            "runtime_worker_id": row.runtime_worker_id,
            "task_id": row.task_id,
            "status": row.status,
            "pid": row.pid,
            "exit_code": row.exit_code,
            "termination_reason": row.termination_reason,
            "startup_error": row.startup_error,
            "cwd": row.cwd,
            "env": row.env,
            "ports": row.ports,
            "network_blocked": row.network_blocked,
            "network_allow_list": row.network_allow_list,
            "gpu": row.gpu,
            "gpu_count": row.gpu_count,
            "timeout_seconds": row.timeout_seconds,
            "expires_at": to_utc_or_none(row.expires_at),
            "created_at": to_utc(row.created_at),
            "started_at": to_utc_or_none(row.started_at),
            "finished_at": to_utc_or_none(row.finished_at),
            "preemption_settled_at": to_utc_or_none(row.preemption_settled_at),
        }
    )


def write_container(row: ContainerTable, record: ContainerRecord) -> None:
    row.name = record.name
    row.image = record.image
    row.command = list(record.command)
    row.workspace_id = record.workspace_id
    row.stub_id = record.stub_id
    row.app_id = record.app_id
    row.machine_id = record.machine_id
    row.worker_id = record.worker_id
    row.runtime_machine_id = record.runtime_machine_id
    row.runtime_worker_id = record.runtime_worker_id
    row.task_id = record.task_id
    row.status = record.status.value
    row.pid = record.pid
    row.exit_code = record.exit_code
    row.termination_reason = record.termination_reason.value
    row.startup_error = record.startup_error
    row.cwd = record.cwd
    row.env = dict(record.env)
    row.ports = dict(record.ports)
    row.network_blocked = record.network_blocked
    row.network_allow_list = list(record.network_allow_list)
    row.gpu = list(record.gpu)
    row.gpu_count = record.gpu_count
    row.timeout_seconds = record.timeout_seconds
    row.expires_at = record.expires_at
    row.started_at = record.started_at
    row.finished_at = record.finished_at
    row.preemption_settled_at = record.preemption_settled_at


def scheduling_request_from_row(
    row: ContainerTable, *, include_payload: bool = True
) -> SchedulerWorkerRequest:
    if row.scheduling_requested_at is None or row.scheduling_placement is None:
        raise ValueError("container has no scheduling request")
    return SchedulerWorkerRequest.model_validate(
        {
            "workspace_id": row.workspace_id,
            "stub_id": row.scheduling_stub_id,
            "deployment_id": row.scheduling_deployment_id,
            "container_id": row.id,
            "cpu_millicores": row.scheduling_cpu_millicores,
            "required_worker_id": row.scheduling_required_worker_id,
            "preferred_worker_id": row.scheduling_preferred_worker_id,
            "memory_mib": row.scheduling_memory_mib,
            "gpu": row.scheduling_gpu,
            "gpu_count": row.scheduling_gpu_count,
            "disk_bytes": row.scheduling_disk_bytes,
            "placement": Placement.parse(row.scheduling_placement),
            "architecture": row.scheduling_architecture,
            "provider_runtime": row.scheduling_provider_runtime,
            "runtime_class": row.scheduling_runtime_class,
            "docker_enabled": row.scheduling_docker_enabled,
            "preemptible": row.scheduling_preemptible,
            "workspace_gpu_quota": row.scheduling_workspace_gpu_quota,
            "workspace_cpu_quota_millicores": row.scheduling_workspace_cpu_quota_millicores,
            "retry_count": row.scheduling_retry_count,
            "timestamp": to_utc(row.scheduling_requested_at),
            "payload": row.scheduling_payload if include_payload else {},
            "backfill": row.scheduling_backfill,
            "region": row.scheduling_region,
            "availability_zone": row.scheduling_availability_zone,
            "capacity_retry_at": to_utc_or_none(row.capacity_retry_at),
        }
    )


def write_scheduling_request(row: ContainerTable, request: SchedulerWorkerRequest) -> None:
    row.scheduling_stub_id = request.stub_id
    row.scheduling_deployment_id = request.deployment_id
    row.scheduling_cpu_millicores = request.cpu_millicores
    row.scheduling_required_worker_id = request.required_worker_id
    row.scheduling_preferred_worker_id = request.preferred_worker_id
    row.scheduling_memory_mib = request.memory_mib
    row.scheduling_gpu = list(request.gpu)
    row.scheduling_gpu_count = request.gpu_count
    row.scheduling_disk_bytes = request.disk_bytes
    row.scheduling_placement = request.placement.key
    row.scheduling_architecture = request.architecture
    row.scheduling_provider_runtime = request.provider_runtime
    row.scheduling_runtime_class = request.runtime_class
    row.scheduling_docker_enabled = request.docker_enabled
    row.scheduling_preemptible = request.preemptible
    row.scheduling_workspace_gpu_quota = request.workspace_gpu_quota
    row.scheduling_workspace_cpu_quota_millicores = request.workspace_cpu_quota_millicores
    row.scheduling_retry_count = request.retry_count
    row.scheduling_requested_at = request.timestamp
    row.scheduling_payload = dict(request.payload)
    row.scheduling_backfill = request.backfill
    row.scheduling_region = request.region
    row.scheduling_availability_zone = request.availability_zone
