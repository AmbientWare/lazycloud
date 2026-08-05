from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError
from shared.capacity import CapacityOwnerKind, CapacityOwnerSource
from shared.compute_policy import ComputeUnitRecord
from shared.containers import ContainerRecord, ContainerStatus
from shared.cron import CronJobRun
from shared.provider_config import ProviderConfig, ProviderKind

_UNIT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_WORKSPACE_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


def _unit(name: str, **overrides: object) -> ComputeUnitRecord:
    return ComputeUnitRecord.model_validate(
        {
            "id": _UNIT_ID,
            "workspace_id": _WORKSPACE_ID,
            "name": name,
            "pool": name,
            **overrides,
        }
    )


def test_compute_pool_capacity_policy_rejects_ambiguous_ownership_and_shape() -> None:
    with pytest.raises(ValidationError, match="requires source 'managed'"):
        _unit(
            "invalid-owner",
            capacity_owner_kind=CapacityOwnerKind.ManagedUnit,
            capacity_owner_source=CapacityOwnerSource.Agent,
        )

    with pytest.raises(ValidationError, match="min <= desired <= max"):
        _unit("invalid-desired", desired_machines=0, min_machines=1, max_machines=2)

    with pytest.raises(ValidationError, match="min <= initial <= max"):
        _unit(
            "invalid-initial",
            desired_machines=1,
            initial_machines=0,
            min_machines=1,
            max_machines=2,
        )

    with pytest.raises(ValidationError, match="positive maximum, worker CPU, and memory"):
        _unit("missing-shape", scaling_enabled=True)

    with pytest.raises(ValidationError, match="GPU type and count"):
        _unit("invalid-gpu", worker_gpu_type="L4")

    with pytest.raises(ValidationError, match="non-empty and unique"):
        _unit("duplicate-runtime", worker_runtimes=("runc", "runc"))

    with pytest.raises(ValidationError, match="minimum free capacity requires scaling"):
        _unit("headroom-without-scaling", min_free_cpu_millicores=1)

    with pytest.raises(ValidationError, match="requires a GPU worker shape"):
        _unit(
            "gpu-headroom-without-gpu",
            scaling_enabled=True,
            max_machines=2,
            worker_cpu_millicores=1_000,
            worker_memory_mib=1_024,
            min_free_gpu_count=1,
        )

    aggregate_headroom = _unit(
        "aggregate-headroom",
        scaling_enabled=True,
        max_machines=4,
        worker_cpu_millicores=1_000,
        worker_memory_mib=1_024,
        min_free_cpu_millicores=3_000,
        min_free_memory_mib=3_072,
    )
    assert aggregate_headroom.min_free_cpu_millicores > aggregate_headroom.worker_cpu_millicores


def test_compute_pool_capacity_owner_id_is_frozen() -> None:
    pool = _unit("immutable-owner")

    with pytest.raises(ValidationError, match="Field is frozen"):
        pool.capacity_owner_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def test_container_and_cron_records_preserve_terminal_state_and_timestamps() -> None:
    now = datetime(2026, 7, 19, tzinfo=timezone.utc)
    container = ContainerRecord(
        id="container-1",
        name="function",
        image="registry.example/function:1",
        command=["python", "-m", "runner.function"],
        workspace_id="workspace-1",
        status=ContainerStatus.Stopped,
        exit_code=0,
        env={"PRIVATE_TOKEN": "persisted-for-worker-only"},
        ports={"8080": 8080},
        timeout_seconds=-1,
        created_at=now,
        started_at=now,
        finished_at=now,
    )
    run = CronJobRun(
        id="run-1",
        workspace_id="workspace-1",
        cron_job="nightly",
        enqueued=True,
        message_id="message-1",
        task_id="task-1",
        created_at=now,
    )

    assert ContainerRecord.model_validate_json(container.model_dump_json()) == container
    assert CronJobRun.model_validate_json(run.model_dump_json()) == run


def test_provider_config_accepts_only_recursive_json_values() -> None:
    provider = ProviderConfig(
        name="primary",
        kind=ProviderKind.Aws,
        priority=0,
        config={
            "region": "us-west-2",
            "capacity": {"on_demand": True, "weights": [0, 1.5]},
            "fallback": None,
        },
        labels={"environment": "production"},
    )

    assert ProviderConfig.model_validate_json(provider.model_dump_json()) == provider
    with pytest.raises(ValidationError):
        ProviderConfig.model_validate(
            {
                "name": "invalid",
                "config": {"created_at": datetime(2026, 7, 19, tzinfo=timezone.utc)},
            }
        )
