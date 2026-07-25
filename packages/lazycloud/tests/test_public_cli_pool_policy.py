from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from shared.capacity import CapacityOwnerKind, CapacityOwnerSource
from shared.http.compute import PoolCreateRequest, PoolListResponse, PoolResponse


@dataclass
class FakePoolResourceClient:
    create_requests: list[PoolCreateRequest] = field(default_factory=list)
    deleted_names: list[str] = field(default_factory=list)

    def _pool(self, request: PoolCreateRequest) -> PoolResponse:
        return PoolResponse(
            capacity_owner_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            capacity_owner_kind=CapacityOwnerKind.WorkspaceAgent,
            capacity_owner_source=CapacityOwnerSource.Agent,
            **request.model_dump(mode="python"),
            created_at=datetime(2026, 7, 21, tzinfo=UTC),
        )

    def list_pools(self) -> PoolListResponse:
        return PoolListResponse(
            pools=[
                self._pool(
                    PoolCreateRequest(
                        name="gpu-pool",
                        provider="agent",
                        initial_workers=1,
                        min_workers=1,
                        max_workers=3,
                        scaling_enabled=True,
                        default_eligible=True,
                        priority=120,
                        min_free_cpu_millicores=8_000,
                        min_free_memory_mib=32_768,
                        min_free_gpu_count=1,
                        worker_cpu_millicores=4_000,
                        worker_memory_mib=16_384,
                        worker_gpu_type="L4",
                        worker_gpu_count=1,
                        worker_runtimes=("runc", "runsc"),
                        worker_preemptible=True,
                        idle_drain_timeout_seconds=90,
                        scale_up_cooldown_seconds=10,
                        scale_down_cooldown_seconds=120,
                        registration_timeout_seconds=120,
                    )
                )
            ]
        )

    def create_pool(self, request: PoolCreateRequest) -> PoolResponse:
        self.create_requests.append(request)
        return self._pool(request)

    def delete_pool(self, name: str) -> None:
        self.deleted_names.append(name)
