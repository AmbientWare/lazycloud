from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import JsonValue
from shared.app_identity import CLI_NAME
from shared.capacity import CapacityOwnerKind, CapacityOwnerSource
from shared.http.compute import (
    PoolCreateRequest,
    PoolJoinCommandRequest,
    PoolJoinTokenRequest,
    PoolListResponse,
    PoolResponse,
)


@dataclass
class FakePrivatePoolGateway:
    list_requests: int = 0
    create_requests: list[PoolCreateRequest] = field(default_factory=list)
    delete_requests: list[str] = field(default_factory=list)

    def list_pools(self) -> PoolListResponse:
        self.list_requests += 1
        return PoolListResponse(
            pools=[
                PoolResponse(
                    capacity_owner_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    capacity_owner_kind=CapacityOwnerKind.WorkspaceAgent,
                    capacity_owner_source=CapacityOwnerSource.Agent,
                    name="gpu-pool",
                    created_at=datetime(2026, 1, 1, tzinfo=UTC),
                )
            ]
        )

    def create_pool(self, request: PoolCreateRequest) -> PoolResponse:
        self.create_requests.append(request)
        return PoolResponse(
            capacity_owner_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            capacity_owner_kind=CapacityOwnerKind.WorkspaceAgent,
            capacity_owner_source=CapacityOwnerSource.Agent,
            **request.model_dump(mode="python"),
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    def delete_pool(self, name: str) -> None:
        self.delete_requests.append(name)


@dataclass
class FakeComputeChannel:
    requests: list[tuple[str, str, dict[str, JsonValue] | None]] = field(default_factory=list)

    def get(self, path: str) -> JsonValue:
        self.requests.append(("GET", path, None))
        return {
            "data": [
                {
                    "id": "machine-1",
                    "pool_name": "gpu-pool",
                    "readiness_phase": "joining",
                }
            ],
            "next": "",
        }

    def post(
        self,
        path: str,
        payload: dict[str, JsonValue] | None = None,
    ) -> JsonValue:
        self.requests.append(("POST", path, payload))
        if path.startswith("/api/v1/pools/gpu-pool/join-token"):
            PoolJoinTokenRequest.model_validate(payload)
            return {"token": "join-token", "expires_at": "2026-01-01T00:30:00Z"}
        PoolJoinCommandRequest.model_validate(payload)
        return {
            "command": f"{CLI_NAME} agent join --token join-token",
            "expires_at": "2026-01-01T00:30:00Z",
        }

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, JsonValue] | None = None,
    ) -> JsonValue:
        self.requests.append((method, path, payload))
        return {}
