from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from shared.http.compute import (
    PoolCreateRequest,
    PoolJoinCommandRequest,
    PoolJoinCommandResponse,
    PoolJoinTokenRequest,
    PoolJoinTokenResponse,
    PoolListResponse,
    PoolMachineResponse,
    PoolResponse,
)

from lazycloud.clients.compute.control import ComputeClient
from lazycloud.control import ControlClientConfigMixin
from lazycloud.control_clients import resource_control_client


class PrivatePoolControlClient(Protocol):
    def list_pools(self) -> PoolListResponse: ...

    def create_pool(self, request: PoolCreateRequest) -> PoolResponse: ...

    def delete_pool(self, name: str) -> None: ...


@dataclass(slots=True)
class PrivatePoolClient(ControlClientConfigMixin):
    client: PrivatePoolControlClient | None = None
    capacity_client: ComputeClient | None = None
    workspace: str | None = None
    endpoint: str | None = None
    token: str | None = None
    timeout_seconds: float = 10.0

    @property
    def control_client(self) -> PrivatePoolControlClient:
        if self.client is None:
            self.client = resource_control_client(self._config())
        return self.client

    @property
    def compute_client(self) -> ComputeClient:
        if self.capacity_client is None:
            config = self._config()
            self.capacity_client = ComputeClient.from_endpoint(
                config.endpoint,
                token=config.token,
                timeout_seconds=config.timeout_seconds,
                workspace=config.workspace,
            )
        return self.capacity_client

    def list(
        self,
        *,
        filters: dict[str, list[str]] | None = None,
        limit: int = 100,
    ) -> list[PoolResponse]:
        pools = self.control_client.list_pools().pools
        selected_names = set((filters or {}).get("name", ()))
        selected = [pool for pool in pools if not selected_names or pool.name in selected_names]
        return selected[:limit]

    def create(
        self,
        name: str,
        *,
        provider: str = "agent",
        initial_workers: int = 0,
        min_workers: int = 0,
        max_workers: int = 1,
        scaling_enabled: bool = False,
        default_eligible: bool = False,
        priority: int = 0,
        min_free_cpu_millicores: int = 0,
        min_free_memory_mib: int = 0,
        min_free_gpu_count: int = 0,
        worker_cpu_millicores: int = 0,
        worker_memory_mib: int = 0,
        worker_gpu_type: str = "",
        worker_gpu_count: int = 0,
        worker_runtimes: tuple[str, ...] = ("runc",),
        worker_preemptible: bool = False,
        idle_drain_timeout_seconds: int = 300,
        scale_up_cooldown_seconds: int = 5,
        scale_down_cooldown_seconds: int = 60,
        registration_timeout_seconds: int = 600,
    ) -> PoolResponse:
        return self.control_client.create_pool(
            PoolCreateRequest(
                name=name,
                provider=provider,
                initial_workers=initial_workers,
                min_workers=min_workers,
                max_workers=max_workers,
                scaling_enabled=scaling_enabled,
                default_eligible=default_eligible,
                priority=priority,
                min_free_cpu_millicores=min_free_cpu_millicores,
                min_free_memory_mib=min_free_memory_mib,
                min_free_gpu_count=min_free_gpu_count,
                worker_cpu_millicores=worker_cpu_millicores,
                worker_memory_mib=worker_memory_mib,
                worker_gpu_type=worker_gpu_type,
                worker_gpu_count=worker_gpu_count,
                worker_runtimes=worker_runtimes,
                worker_preemptible=worker_preemptible,
                idle_drain_timeout_seconds=idle_drain_timeout_seconds,
                scale_up_cooldown_seconds=scale_up_cooldown_seconds,
                scale_down_cooldown_seconds=scale_down_cooldown_seconds,
                registration_timeout_seconds=registration_timeout_seconds,
            )
        )

    def delete(self, name: str) -> None:
        self.control_client.delete_pool(name)

    def join_token(self, pool_name: str, *, ttl: str = "") -> PoolJoinTokenResponse:
        response = self.compute_client.create_pool_join_token(
            pool_name,
            PoolJoinTokenRequest(ttl=ttl),
        )
        return response

    def revoke_join_token(self, pool_name: str) -> None:
        self.compute_client.revoke_pool_join_token(pool_name)

    def join_command(
        self,
        pool_name: str,
        *,
        ttl: str = "",
    ) -> str:
        return self.join(pool_name, ttl=ttl).command

    def join(
        self,
        pool_name: str,
        *,
        ttl: str = "",
    ) -> PoolJoinCommandResponse:
        response = self.compute_client.pool_join_command(
            pool_name,
            PoolJoinCommandRequest(ttl=ttl),
        )
        return response

    def machines(self, pool_name: str, *, limit: int = 100) -> list[PoolMachineResponse]:
        return self.compute_client.list_pool_machines(pool_name, limit=limit).data


__all__ = [
    "PrivatePoolClient",
    "PrivatePoolControlClient",
]
