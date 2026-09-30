from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from shared.capacity import CapacityOwnerKind, CapacityOwnerSource
from shared.http.compute import UnitResponse, WorkerListResponse
from shared.http.pods import PodSandboxExposePortRequest
from shared.placement import Placement

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize("port", [0, 65536, True, 8080.0, "8080"])
def test_sandbox_exposed_port_rejects_out_of_range_and_coerced_values(port: object) -> None:
    with pytest.raises(ValidationError):
        PodSandboxExposePortRequest.model_validate({"port": port})


def test_canonical_worker_and_pool_views_preserve_nominal_json_contracts() -> None:
    response = WorkerListResponse.model_validate(
        {
            "workers": [
                {
                    "id": "worker-1",
                    "status": "available",
                    "placement": Placement.platform(),
                    "machine_id": "machine-1",
                    "created_at": NOW,
                    "updated_at": NOW,
                }
            ]
        }
    )
    pool = UnitResponse(
        id="cu_01J8Z9QK2M0000000000000000",
        name="default",
        placement=Placement.platform(),
        provider="agent",
        capacity_owner_id="6fb19db5-ddd0-478d-8f4a-cdf422ad438c",
        capacity_owner_kind=CapacityOwnerKind.WorkspaceAgent,
        capacity_owner_source=CapacityOwnerSource.Agent,
        labels={"region": "local"},
        created_at=NOW,
    )

    assert WorkerListResponse.model_validate_json(response.model_dump_json()) == response
    assert UnitResponse.model_validate_json(pool.model_dump_json()) == pool
