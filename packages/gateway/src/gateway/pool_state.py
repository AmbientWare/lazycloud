from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from compute.agent_control import (
    ComputePrincipal,
    JoinTokenCreationPlan,
    plan_join_token_creation,
)
from compute.projection import PrivatePoolState
from compute.state import (
    ComputeJoinTokenState,
    ComputePoolState,
    RedisComputeStateRepository,
)
from database.context import ServiceContext
from database.repositories.compute import ComputeJoinCredentialRepository
from foundation.ids import try_uuid
from pydantic import JsonValue, TypeAdapter
from shared.compute_enrollment import ComputeCredentialStatus
from shared.compute_fleet import Machine
from shared.compute_policy import ComputePoolRecord
from shared.errors import InvalidInputError, NotFoundError
from shared.routing import BackendRouteTransport, PrivatePoolFallback
from shared.timestamps import utc_now

from compute import projection
from gateway.views import pool_config_from_pool, private_pool_from_compute_state

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


class GatewayComputeService(Protocol):
    def list_pools(self, *, workspace: str = "default") -> Iterable[ComputePoolRecord]: ...

    def list_machines(self, *, workspace: str = "default") -> Iterable[Machine]: ...

    def create_pool(
        self,
        name: str,
        *,
        provider: str,
        min_machines: int,
        max_machines: int,
        worker_gpu_type: str = "",
        worker_gpu_count: int = 0,
        priority: int = 0,
        transport: BackendRouteTransport = BackendRouteTransport.TsnetRestricted,
        fallback: PrivatePoolFallback = PrivatePoolFallback.Internal,
        workspace: str = "default",
    ) -> ComputePoolRecord: ...


@dataclass(slots=True)
class GatewayPoolStateCoordinator:
    context: ServiceContext
    compute: GatewayComputeService
    compute_states: RedisComputeStateRepository

    def pool_by_name(self, name: str, *, workspace_id: str) -> ComputePoolRecord:
        for pool in self.compute.list_pools(workspace=workspace_id):
            if pool.name == name:
                return pool
        msg = f"pool not found: {name}"
        raise NotFoundError(msg)

    def create_or_update_pool(
        self,
        config: projection.PoolConfig,
        *,
        workspace_id: str,
    ) -> ComputePoolRecord:
        if not config.name:
            msg = "pool name is required"
            raise InvalidInputError(msg)
        try:
            normalized = projection.normalize_pool_config(config)
            projection.compute_pool_from_config(config)
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc
        if normalized is None:
            msg = "pool config is required"
            raise InvalidInputError(msg)
        provider = (normalized.providers[0] if normalized.providers else "") or "agent"
        gpu_type = normalized.gpu[0] if normalized.gpu else ""
        return self.compute.create_pool(
            normalized.name,
            provider=provider,
            min_machines=0,
            max_machines=max(normalized.nodes, 1),
            worker_gpu_type=gpu_type,
            worker_gpu_count=1 if gpu_type else 0,
            priority=normalized.priority,
            transport=normalized.transport,
            fallback=normalized.fallback,
            workspace=workspace_id,
        )

    def ensure_compute_pool_state(
        self,
        pool: ComputePoolRecord,
        *,
        workspace_id: str,
        config: projection.PoolConfig | None = None,
        owner_token_id: str = "gateway",
    ) -> PrivatePoolState:
        current = self.compute_states.get_pool_state(workspace_id, pool.name)
        compute_config = config or pool_config_from_pool(pool)
        metadata: dict[str, JsonValue] = {
            **(current.metadata if current is not None else {}),
            "config": _JSON_OBJECT.validate_python(compute_config.model_dump(mode="json")),
            "created_by_token_id": (
                str((current.metadata if current else {}).get("created_by_token_id") or "")
                or owner_token_id
            ),
        }
        state = ComputePoolState(
            workspace_id=workspace_id,
            name=pool.name,
            capacity_owner_id=pool.capacity_owner_id,
            provider=pool.provider,
            max_machines=max(pool.max_machines, 1),
            desired_machines=pool.max_machines,
            active_machines=len(
                [
                    machine
                    for machine in self.compute.list_machines(workspace=workspace_id)
                    if machine.pool == pool.name
                ]
            ),
            metadata=metadata,
        )
        self.compute_states.save_pool_state(state)
        return private_pool_from_compute_state(state)

    def private_pool_for_join_token(
        self,
        token_state: ComputeJoinTokenState | None,
    ) -> PrivatePoolState | None:
        if token_state is None:
            return None
        return self.private_pool_by_name(
            token_state.pool_name,
            workspace_id=token_state.workspace_id,
            owner_token_id=token_state.created_by_token_id or "gateway",
        )

    def private_pool_by_name(
        self,
        pool_name: str,
        *,
        workspace_id: str,
        owner_token_id: str = "gateway",
    ) -> PrivatePoolState:
        state = self.compute_states.get_pool_state(workspace_id, pool_name)
        if state is not None:
            return private_pool_from_compute_state(state)
        pool = self.pool_by_name(pool_name, workspace_id=workspace_id)
        return self.ensure_compute_pool_state(
            pool,
            workspace_id=workspace_id,
            owner_token_id=owner_token_id,
        )

    def create_pool_join_token(
        self,
        pool_name: str,
        *,
        workspace_id: str,
        owner_token_id: str,
        ttl: str = "",
    ) -> JoinTokenCreationPlan:
        plan = self.plan_pool_join_token(
            pool_name,
            workspace_id=workspace_id,
            owner_token_id=owner_token_id,
            ttl=ttl,
        )
        current_time = utc_now()
        with self.context.database.session() as session:
            credentials = ComputeJoinCredentialRepository(session)
            if not credentials.lock_pool(workspace_id, pool_name):
                raise NotFoundError(f"pool not found: {pool_name}")
            previous = credentials.list_for_pool(workspace_id, pool_name, for_update=True)
            for credential in previous:
                if (
                    credential.status is ComputeCredentialStatus.Active
                    and credential.machine_id == ""
                ):
                    credentials.save(credential.revoke(now=current_time))
            durable = credentials.create(
                token_hash=plan.token_hash,
                workspace_id=workspace_id,
                pool_name=pool_name,
                created_by_token_id=try_uuid(owner_token_id),
                max_uses=plan.state.max_uses,
                expires_at=plan.expires_at,
            )
        for credential in previous:
            if credential.status is ComputeCredentialStatus.Active and credential.machine_id == "":
                self.compute_states.revoke_join_token_state(credential.token_hash)
        token_state = plan.state.model_copy(
            update={
                "credential_id": durable.id,
                "created_by_token_id": durable.created_by_token_id or owner_token_id,
                "max_uses": durable.max_uses,
                "use_count": durable.use_count,
            }
        )
        self.compute_states.save_join_token_state(token_state, ttl_seconds=plan.ttl_seconds)
        return plan

    def revoke_pool_join_token(self, pool_name: str, *, workspace_id: str) -> None:
        current_time = utc_now()
        with self.context.database.session() as session:
            credentials = ComputeJoinCredentialRepository(session)
            if not credentials.lock_pool(workspace_id, pool_name):
                raise NotFoundError(f"pool not found: {pool_name}")
            records = credentials.list_for_pool(workspace_id, pool_name, for_update=True)
            active = [
                item
                for item in records
                if item.status is ComputeCredentialStatus.Active and item.machine_id == ""
            ]
            for credential in active:
                credentials.save(credential.revoke(now=current_time))
        for credential in active:
            self.compute_states.revoke_join_token_state(credential.token_hash)

    def plan_pool_join_token(
        self,
        pool_name: str,
        *,
        workspace_id: str,
        owner_token_id: str,
        ttl: str = "",
    ) -> JoinTokenCreationPlan:
        try:
            pool = self.pool_by_name(pool_name, workspace_id=workspace_id)
            pool_state = self.ensure_compute_pool_state(
                pool,
                workspace_id=workspace_id,
                owner_token_id=owner_token_id,
            )
            return plan_join_token_creation(
                ComputePrincipal(
                    workspace_id=pool_state.workspace_id or "default",
                    owner_token_id=pool_state.created_by_token_id or "gateway",
                ),
                pool_name,
                ttl=ttl,
                max_uses=1,
            )
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc

    def save_compute_pool_config_update(
        self,
        workspace_id: str,
        pool_state: PrivatePoolState,
        config: projection.PoolConfig,
    ) -> None:
        current = self.compute_states.get_pool_state(workspace_id, pool_state.name)
        metadata: dict[str, JsonValue] = {
            **(current.metadata if current is not None else {}),
            "config": _JSON_OBJECT.validate_python(config.model_dump(mode="json")),
            "created_by_token_id": pool_state.created_by_token_id or "gateway",
        }
        state = ComputePoolState(
            workspace_id=workspace_id,
            name=pool_state.name,
            capacity_owner_id=(
                current.capacity_owner_id if current is not None else pool_state.capacity_owner_id
            ),
            provider=(current.provider if current is not None else "agent"),
            max_machines=current.max_machines if current is not None else max(config.nodes, 1),
            desired_machines=(
                current.desired_machines if current is not None else max(config.nodes, 1)
            ),
            active_machines=current.active_machines if current is not None else 0,
            metadata=metadata,
        )
        self.compute_states.save_pool_state(state)

    def delete_compute_pool_state(self, pool_name: str, *, workspace_id: str) -> bool:
        return self.compute_states.delete_pool_state(workspace_id, pool_name)


__all__ = ["GatewayComputeService", "GatewayPoolStateCoordinator"]
