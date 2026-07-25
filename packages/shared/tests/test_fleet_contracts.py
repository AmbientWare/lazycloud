from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError
from shared.capacity import CapacityOwnerKind, CapacityOwnerSource
from shared.compute_fleet import (
    Pool,
)
from shared.containers import ContainerRecord, ContainerStatus
from shared.cron import CronJobRun
from shared.provider_config import ProviderConfig, ProviderKind


def test_compute_pool_capacity_policy_rejects_ambiguous_ownership_and_shape() -> None:
    with pytest.raises(ValidationError, match="requires source 'kubernetes'"):
        Pool(
            capacity_owner_kind=CapacityOwnerKind.GlobalKubernetesDeployment,
            capacity_owner_source=CapacityOwnerSource.Agent,
            name="invalid-owner",
        )

    with pytest.raises(ValidationError, match="min <= initial <= max"):
        Pool(name="invalid-bounds", initial_workers=0, min_workers=1, max_workers=2)

    with pytest.raises(ValidationError, match="positive maximum, worker CPU, and memory"):
        Pool(name="missing-shape", scaling_enabled=True)

    with pytest.raises(ValidationError, match="GPU type and count"):
        Pool(name="invalid-gpu", worker_gpu_type="L4")

    with pytest.raises(ValidationError, match="non-empty and unique"):
        Pool(name="duplicate-runtime", worker_runtimes=("runc", "runc"))

    with pytest.raises(ValidationError, match="minimum free capacity requires scaling"):
        Pool(name="headroom-without-scaling", min_free_cpu_millicores=1)

    with pytest.raises(ValidationError, match="requires a GPU worker shape"):
        Pool(
            name="gpu-headroom-without-gpu",
            scaling_enabled=True,
            max_workers=2,
            worker_cpu_millicores=1_000,
            worker_memory_mib=1_024,
            min_free_gpu_count=1,
        )

    aggregate_headroom = Pool(
        name="aggregate-headroom",
        scaling_enabled=True,
        max_workers=4,
        worker_cpu_millicores=1_000,
        worker_memory_mib=1_024,
        min_free_cpu_millicores=3_000,
        min_free_memory_mib=3_072,
    )
    assert aggregate_headroom.min_free_cpu_millicores > aggregate_headroom.worker_cpu_millicores


def test_compute_pool_capacity_owner_id_is_frozen() -> None:
    pool = Pool(name="immutable-owner")

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
