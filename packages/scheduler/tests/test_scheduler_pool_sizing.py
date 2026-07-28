from __future__ import annotations

from datetime import UTC, datetime, timedelta

from scheduler.pool_sizing import (
    WorkerPoolSizingAction,
    effective_pool_headroom,
    plan_worker_pool_sizing,
)
from shared.capacity import CapacityOwnerKind, CapacityOwnerSource, CapacityPoolSizingState
from shared.compute_fleet import Pool
from shared.scheduling import SchedulerWorkerRecord, SchedulerWorkerStatus

OWNER_ID = "11111111-1111-4111-8111-111111111111"
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _pool(**updates: object) -> Pool:
    values: dict[str, object] = {
        "capacity_owner_id": OWNER_ID,
        "capacity_owner_kind": CapacityOwnerKind.ManagedPool,
        "capacity_owner_source": CapacityOwnerSource.Managed,
        "name": "cpu",
        "provider": "managed",
        "initial_workers": 2,
        "min_workers": 1,
        "max_workers": 4,
        "scaling_enabled": True,
        "default_eligible": True,
        "min_free_cpu_millicores": 2_000,
        "min_free_memory_mib": 2_048,
        "worker_cpu_millicores": 4_000,
        "worker_memory_mib": 8_192,
    }
    values.update(updates)
    return Pool.model_validate(values)


def _state(**updates: object) -> CapacityPoolSizingState:
    values: dict[str, object] = {
        "capacity_owner_id": OWNER_ID,
        "pool_name": "cpu",
        "workspace_id": "workspace-1",
    }
    values.update(updates)
    return CapacityPoolSizingState.model_validate(values)


def _worker(
    worker_id: str,
    status: SchedulerWorkerStatus,
    *,
    free_cpu: int = 4_000,
    free_memory: int = 8_192,
    total_memory: int = 8_192,
) -> SchedulerWorkerRecord:
    return SchedulerWorkerRecord(
        worker_id=worker_id,
        capacity_owner_id=OWNER_ID,
        pool_name="cpu",
        status=status,
        runtime_classes=["runc"],
        free_cpu_millicores=free_cpu,
        free_memory_mib=free_memory,
        total_cpu_millicores=4_000,
        total_memory_mib=total_memory,
    )


def test_effective_headroom_counts_available_and_unclaimed_pending_then_allocations() -> None:
    class Reservation:
        capacity_owner_id = OWNER_ID
        target_worker_id = "pending-claimed"
        desired_unit = 1
        open = True

    class Allocation:
        cpu_millicores = 1_000
        memory_mib = 1_024
        gpu_count = 0

    headroom = effective_pool_headroom(
        _pool(),
        [
            _worker(
                "available",
                SchedulerWorkerStatus.Available,
                free_memory=7_900,
                total_memory=7_900,
            ),
            _worker("pending-free", SchedulerWorkerStatus.Pending),
            _worker("pending-claimed", SchedulerWorkerStatus.Pending),
            _worker("cordoned", SchedulerWorkerStatus.Unavailable),
        ],
        reservations=[Reservation()],
        allocations=[Allocation()],
    )

    assert headroom.available_workers == 1
    assert headroom.unclaimed_pending_workers == 1
    assert headroom.active_allocations == 1
    assert headroom.cpu_millicores == 7_000
    assert headroom.memory_mib == 15_068


def test_initial_floor_and_free_headroom_request_only_one_unit_per_reconcile() -> None:
    headroom = effective_pool_headroom(_pool(), [])

    initial = plan_worker_pool_sizing(
        _pool(),
        headroom=headroom,
        registered_units=0,
        authoritative_units=0,
        state=_state(),
        now=NOW,
    )
    below_headroom = plan_worker_pool_sizing(
        _pool(initial_workers=0, min_workers=0),
        headroom=headroom,
        registered_units=2,
        authoritative_units=2,
        state=_state(initial_target_reached=True),
        now=NOW,
    )

    assert initial.action is WorkerPoolSizingAction.ScaleUp
    assert initial.target_units == 1
    assert initial.reason == "worker count is below the configured baseline"
    assert below_headroom.action is WorkerPoolSizingAction.ScaleUp
    assert below_headroom.target_units == 3
    assert below_headroom.reason == "effective free headroom is below the configured minimum"


def test_pending_target_and_durable_cooldown_prevent_duplicate_scale_up() -> None:
    headroom = effective_pool_headroom(_pool(), [])
    waiting_registration = plan_worker_pool_sizing(
        _pool(),
        headroom=headroom,
        registered_units=0,
        authoritative_units=1,
        state=_state(operation_id="operation-1", target_units=1),
        now=NOW,
    )
    cooldown = plan_worker_pool_sizing(
        _pool(initial_workers=0, min_workers=0, scale_up_cooldown_seconds=30),
        headroom=headroom,
        registered_units=1,
        authoritative_units=1,
        state=_state(
            initial_target_reached=True,
            last_scale_up_at=NOW - timedelta(seconds=5),
        ),
        now=NOW,
    )

    assert waiting_registration.action is WorkerPoolSizingAction.Wait
    assert waiting_registration.target_units == 1
    assert cooldown.action is WorkerPoolSizingAction.Wait
    assert cooldown.retry_after_seconds == 25
