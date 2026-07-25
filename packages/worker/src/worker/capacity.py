from __future__ import annotations

from enum import StrEnum

from shared.contracts import ContractModel

DEFAULT_CONTAINER_STARTUP_TIMEOUT_SECONDS = 300
MAX_CONTAINER_STARTUP_TIMEOUT_SECONDS = 3_600


class WorkerStartupTimeoutSource(StrEnum):
    Default = "default"
    FailoverConfig = "failover-config"
    ClampedMax = "clamped-max"


class WorkerStartupTimeoutPlan(ContractModel):
    timeout_seconds: float
    timeout_milliseconds: int
    source: WorkerStartupTimeoutSource
    clamped: bool = False


def plan_container_startup_timeout(
    max_scheduling_latency_ms: int = 0,
) -> WorkerStartupTimeoutPlan:
    if max_scheduling_latency_ms <= 0:
        return WorkerStartupTimeoutPlan(
            timeout_seconds=float(DEFAULT_CONTAINER_STARTUP_TIMEOUT_SECONDS),
            timeout_milliseconds=DEFAULT_CONTAINER_STARTUP_TIMEOUT_SECONDS * 1000,
            source=WorkerStartupTimeoutSource.Default,
        )
    max_ms = MAX_CONTAINER_STARTUP_TIMEOUT_SECONDS * 1000
    if max_scheduling_latency_ms > max_ms:
        return WorkerStartupTimeoutPlan(
            timeout_seconds=float(MAX_CONTAINER_STARTUP_TIMEOUT_SECONDS),
            timeout_milliseconds=max_ms,
            source=WorkerStartupTimeoutSource.ClampedMax,
            clamped=True,
        )
    return WorkerStartupTimeoutPlan(
        timeout_seconds=max_scheduling_latency_ms / 1000,
        timeout_milliseconds=max_scheduling_latency_ms,
        source=WorkerStartupTimeoutSource.FailoverConfig,
    )
