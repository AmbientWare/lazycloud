from __future__ import annotations

from coordination.redis_client import RedisClient
from database.records.apps import StubRecord
from pydantic import Field, JsonValue
from shared.contracts import ContractModel
from shared.scheduling import gpu_count_for_capacity
from shared.workload_config import StubRuntimeConfig

from scheduler.state import (
    RedisSchedulerContainerRepository,
)


class AutoscalerGuardrailPlan(ContractModel):
    current_count: int = 0
    original_desired_count: int = 0
    desired_count: int = 0
    requested_start_count: int = 0
    available_start_count: int = 0
    limited: bool = False
    reason: str = ""
    enforced: bool = False
    cpu_limit_millicores: int = 0
    cpu_used_millicores: int = 0
    cpu_per_container_millicores: int = 0
    gpu_limit: int = 0
    gpu_used: int = 0
    gpu_per_container: int = 0
    limiting_resources: list[str] = Field(default_factory=list)

    def payload(self) -> dict[str, JsonValue]:
        if not self.enforced and not self.limited:
            return {}
        return self.model_dump(mode="json")


def plan_autoscaler_start_guardrails(
    redis: RedisClient,
    *,
    stub: StubRecord,
    current_count: int,
    desired_count: int,
) -> AutoscalerGuardrailPlan:
    requested_start_count = max(desired_count - current_count, 0)
    base = AutoscalerGuardrailPlan(
        current_count=current_count,
        original_desired_count=desired_count,
        desired_count=desired_count,
        requested_start_count=requested_start_count,
        available_start_count=requested_start_count,
    )
    if requested_start_count <= 0:
        return base

    runtime_config = stub.config.runtime
    cpu_limit = runtime_config.cpu_limit_millicores
    gpu_limit = runtime_config.gpu_limit
    cpu_per_container = runtime_config.cpu_millicores or (
        int(float(runtime_config.cpu) * 1000) if runtime_config.cpu is not None else 0
    )
    gpu_per_container = _gpu_per_container(runtime_config)
    if cpu_limit <= 0 and gpu_limit <= 0:
        return base

    counter = RedisSchedulerContainerRepository(redis).ensure_workspace_concurrency_counter(
        stub.workspace_id
    )
    resource_caps: list[tuple[str, int]] = []
    if cpu_limit > 0 and cpu_per_container > 0:
        resource_caps.append(("cpu", (cpu_limit - counter.cpu_millicores) // cpu_per_container))
    if gpu_limit > 0 and gpu_per_container > 0:
        resource_caps.append(("gpu", (gpu_limit - counter.gpu_count) // gpu_per_container))
    if not resource_caps:
        return base.model_copy(
            update={
                "enforced": True,
                "cpu_limit_millicores": cpu_limit,
                "cpu_used_millicores": counter.cpu_millicores,
                "cpu_per_container_millicores": cpu_per_container,
                "gpu_limit": gpu_limit,
                "gpu_used": counter.gpu_count,
                "gpu_per_container": gpu_per_container,
            }
        )

    available_start_count = max(min(available for _resource, available in resource_caps), 0)
    limiting_resources = [
        resource
        for resource, available in resource_caps
        if max(available, 0) == available_start_count
        and available_start_count < requested_start_count
    ]
    limited = available_start_count < requested_start_count
    next_desired_count = current_count + min(requested_start_count, available_start_count)
    return base.model_copy(
        update={
            "desired_count": next_desired_count,
            "available_start_count": available_start_count,
            "limited": limited,
            "reason": _guardrail_reason(limiting_resources) if limited else "",
            "enforced": True,
            "cpu_limit_millicores": cpu_limit,
            "cpu_used_millicores": counter.cpu_millicores,
            "cpu_per_container_millicores": cpu_per_container,
            "gpu_limit": gpu_limit,
            "gpu_used": counter.gpu_count,
            "gpu_per_container": gpu_per_container,
            "limiting_resources": limiting_resources,
        }
    )


def _gpu_per_container(runtime_config: StubRuntimeConfig) -> int:
    return gpu_count_for_capacity(
        runtime_config.gpu or runtime_config.gpu_type or "",
        runtime_config.gpu_request,
        runtime_config.gpu_count,
    )


def _guardrail_reason(resources: list[str]) -> str:
    if resources == ["cpu"]:
        return "workspace cpu quota reached"
    if resources == ["gpu"]:
        return "workspace gpu quota reached"
    return "workspace resource quota reached"
