from __future__ import annotations

import pytest
from pydantic import ValidationError
from shared.capacity import CapacityOwnerKind, CapacityOwnerSource
from shared.compute_policy import ComputeUnitRecord

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
    with pytest.raises(ValidationError, match="requires source 'provider'"):
        _unit(
            "invalid-owner",
            capacity_owner_kind=CapacityOwnerKind.PooledProvider,
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
        _unit("duplicate-runtime", worker_runtimes=("runsc", "runsc"))

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
