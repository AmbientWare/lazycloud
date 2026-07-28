from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter, ValidationError
from shared.bytes_transport import EncodedBytesBody
from shared.capacity import CapacityOwnerKind, CapacityOwnerSource
from shared.function_payloads import (
    FunctionJsonResult,
    FunctionPayloadEncoding,
    FunctionResultPayload,
)
from shared.http.compute import PoolResponse, WorkerListResponse
from shared.http.gateway import (
    GetOrCreateStubRequest,
    SyncContainerWorkspaceBody,
)
from shared.http.operations import CronJobResponse
from shared.http.pods import PodSandboxExposePortRequest
from shared.http.stubs import StubConfigUpdateRequest
from shared.http.tasks import TaskResponse

NOW = datetime(2026, 1, 1, tzinfo=UTC)


class _ResultBytes(EncodedBytesBody):
    pass


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (GetOrCreateStubRequest, {"name": "function", "metadata": {"bad": object()}}),
        (
            SyncContainerWorkspaceBody,
            {
                "container_id": "container-1",
                "operation": "write",
                "metadata": {"bad": object()},
            },
        ),
        (
            CronJobResponse,
            {
                "name": "nightly",
                "cron": "0 0 * * *",
                "deployment_id": "deployment-1",
                "payload": object(),
                "created_at": NOW,
                "updated_at": NOW,
            },
        ),
        (
            TaskResponse,
            {
                "id": "task-1",
                "name": "task",
                "args": [object()],
                "created_at": NOW,
            },
        ),
        (
            StubConfigUpdateRequest,
            {"inputs": {"bad": object()}},
        ),
    ],
)
def test_http_json_contracts_reject_arbitrary_python(
    model: type[GetOrCreateStubRequest]
    | type[SyncContainerWorkspaceBody]
    | type[CronJobResponse]
    | type[TaskResponse]
    | type[StubConfigUpdateRequest],
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_discriminated_results_and_encoded_bytes_are_precise() -> None:
    adapter: TypeAdapter[FunctionResultPayload] = TypeAdapter(FunctionResultPayload)
    payload: FunctionResultPayload = adapter.validate_python(
        {"encoding": "json", "value": {"answer": 42}}
    )

    assert payload == FunctionJsonResult(value={"answer": 42})
    assert payload.encoding is FunctionPayloadEncoding.Json
    assert adapter.dump_python(payload, mode="json") == {
        "version": 1,
        "encoding": "json",
        "value": {"answer": 42},
    }
    body = _ResultBytes.from_bytes(b"result")
    assert body.bytes_value() == b"result"


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
                    "pool_name": "default",
                    "machine_id": "machine-1",
                    "created_at": NOW,
                    "updated_at": NOW,
                }
            ]
        }
    )
    pool = PoolResponse(
        name="default",
        provider="agent",
        capacity_owner_id="6fb19db5-ddd0-478d-8f4a-cdf422ad438c",
        capacity_owner_kind=CapacityOwnerKind.WorkspaceAgent,
        capacity_owner_source=CapacityOwnerSource.Agent,
        labels={"region": "local"},
        created_at=NOW,
    )

    assert WorkerListResponse.model_validate_json(response.model_dump_json()) == response
    assert PoolResponse.model_validate_json(pool.model_dump_json()) == pool
