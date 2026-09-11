from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol

from compute.agent_control import MachineWorkerAvailability, agent_machine_worker_id
from compute.state import (
    ComputeAgentTokenState,
    ComputeJoinTokenState,
    RedisComputeStateRepository,
)
from coordination.redis_client import RedisClient
from pydantic import JsonValue
from scheduler.compute_hooks import SchedulerComputeHooks
from scheduler.fleet import SchedulerWorkerStatus
from scheduler.state import RedisSchedulerWorkerRepository, SchedulerWorkerRecord
from shared.compute_policy import MachinePool
from shared.timestamps import utc_now


def test_compute_observation_does_not_treat_missing_intake_or_drain_as_machine_failure(
    real_redis_actors: _RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    workers = RedisSchedulerWorkerRepository(redis)
    hooks = SchedulerComputeHooks(RedisComputeStateRepository(redis), workers)
    machine_id = "intake-machine"
    worker = SchedulerWorkerRecord(
        worker_id=agent_machine_worker_id(machine_id),
        capacity_owner_id="11111111-1111-4111-8111-111111111111",
        machine_id=machine_id,
        pool=MachinePool("default"),
        status=SchedulerWorkerStatus.Available,
        request_poll_expires_at=utc_now() - timedelta(seconds=1),
    )
    workers.add_worker(worker)
    assert hooks.machine_worker_availability(machine_id) is MachineWorkerAvailability.Unknown
    worker.request_poll_expires_at = utc_now() + timedelta(seconds=60)
    workers.add_worker(worker)
    assert hooks.machine_worker_availability(machine_id) is MachineWorkerAvailability.Available
    worker.status = SchedulerWorkerStatus.Draining
    workers.add_worker(worker)
    assert hooks.machine_worker_availability(machine_id) is MachineWorkerAvailability.Unknown
    hooks.disable_machine(machine_id, "worker failed")
    assert hooks.machine_worker_availability(machine_id) is MachineWorkerAvailability.Unavailable


class _RealRedisActors(Protocol):
    def client(self) -> RedisClient: ...


def test_scheduler_compute_hooks_disable_machine_workers(
    real_redis_actors: _RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    workers = RedisSchedulerWorkerRepository(redis)
    hooks = SchedulerComputeHooks(RedisComputeStateRepository(redis), workers)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    workers.add_worker(
        SchedulerWorkerRecord(
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            worker_id="worker-machine-one",
            pool=MachinePool("gpu-pool"),
            machine_id="machine-one",
            status=SchedulerWorkerStatus.Available,
            total_cpu_millicores=4000,
            total_memory_mib=8192,
            free_cpu_millicores=4000,
            free_memory_mib=8192,
            created_at=now,
            updated_at=now,
        ),
        now=now,
    )
    fallback_worker_id = agent_machine_worker_id("machine-two")
    workers.add_worker(
        SchedulerWorkerRecord(
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            worker_id=fallback_worker_id,
            pool=MachinePool("gpu-pool"),
            machine_id="",
            status=SchedulerWorkerStatus.Available,
            total_cpu_millicores=4000,
            total_memory_mib=8192,
            free_cpu_millicores=4000,
            free_memory_mib=8192,
            created_at=now,
            updated_at=now,
        ),
        now=now,
    )

    hooks.disable_machine("machine-one", "provider terminated")
    hooks.disable_machine("machine-two", "provider terminated")

    machine_worker = workers.get_worker("worker-machine-one")
    fallback_worker = workers.get_worker(fallback_worker_id)
    assert machine_worker is not None
    assert fallback_worker is not None
    assert machine_worker.status is SchedulerWorkerStatus.Unavailable
    assert fallback_worker.status is SchedulerWorkerStatus.Unavailable


def test_scheduler_compute_hooks_retire_provider_machine_hot_state(
    real_redis_actors: _RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    compute_states = RedisComputeStateRepository(redis)
    workers = RedisSchedulerWorkerRepository(redis)
    hooks = SchedulerComputeHooks(compute_states, workers)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    machine_id = "machine-one"
    worker_id = agent_machine_worker_id(machine_id)
    compute_states.save_agent_token_state(
        ComputeAgentTokenState(
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            token_hash="agent-token-hash",
            workspace_id="ws-1",
            pool=MachinePool("aws-pool"),
            machine_id=machine_id,
            executor="docker",
            preflight_passed=True,
            heartbeat_confirmed=True,
            schedulable=True,
            last_join_at=now,
            last_heartbeat_at=now,
        )
    )
    compute_states.save_join_token_state(
        ComputeJoinTokenState(
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            token_hash="join-token-hash",
            workspace_id="ws-1",
            pool=MachinePool("aws-pool"),
            expires_at=datetime(2026, 1, 2, tzinfo=UTC),
        )
    )
    workers.add_worker(
        SchedulerWorkerRecord(
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            worker_id=worker_id,
            pool=MachinePool("aws-pool"),
            machine_id=machine_id,
            status=SchedulerWorkerStatus.Available,
            created_at=now,
            updated_at=now,
        ),
        now=now,
    )

    hooks.retire_machine("ws-1", machine_id, "provider unit deleted")
    hooks.revoke_unit_join_token("join-token-hash")

    assert compute_states.get_agent_machine_state("ws-1", "aws-pool", machine_id) is None
    assert compute_states.list_agent_token_states("ws-1", "aws-pool") == []
    worker = workers.get_worker(worker_id)
    assert worker is not None
    assert worker.status is SchedulerWorkerStatus.Unavailable
    join_token = compute_states.get_join_token_state("join-token-hash")
    assert join_token is not None
    assert join_token.revoked is True


def _json_object(value: JsonValue, name: str) -> dict[str, JsonValue]:
    assert isinstance(value, dict), f"{name} must be a JSON object"
    return value
