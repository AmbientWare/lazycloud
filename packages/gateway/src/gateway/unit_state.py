from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from compute.agent_control import (
    ComputePrincipal,
    JoinTokenCreationPlan,
    plan_join_token_creation,
)
from compute.projection import PrivateUnitState
from compute.state import (
    ComputeJoinTokenState,
    ComputeUnitState,
    RedisComputeStateRepository,
)
from database.context import ServiceContext
from database.repositories.compute import (
    ComputeJoinCredentialRepository,
    ComputeMachineEnrollmentRepository,
)
from foundation.ids import try_uuid
from pydantic import JsonValue, TypeAdapter
from shared.compute_enrollment import ComputeCredentialStatus
from shared.compute_fleet import Machine
from shared.compute_policy import ComputeUnitRecord, MachinePool, UnitName
from shared.errors import InvalidInputError, NotFoundError
from shared.routing import BackendRouteTransport, PrivateUnitFallback
from shared.timestamps import utc_now

from compute import projection
from gateway.views import pool_config_from_unit, private_pool_from_compute_state

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


class GatewayComputeService(Protocol):
    def list_units(self, *, workspace: str = "default") -> Iterable[ComputeUnitRecord]: ...

    def list_machines(self, *, workspace: str = "default") -> Iterable[Machine]: ...

    def create_unit(
        self,
        name: UnitName,
        *,
        pool: MachinePool | None = None,
        provider: str,
        min_machines: int,
        max_machines: int,
        worker_gpu_type: str = "",
        worker_gpu_count: int = 0,
        priority: int = 0,
        transport: BackendRouteTransport = BackendRouteTransport.TsnetRestricted,
        fallback: PrivateUnitFallback = PrivateUnitFallback.Internal,
        workspace: str = "default",
    ) -> ComputeUnitRecord: ...


@dataclass(slots=True)
class GatewayUnitStateCoordinator:
    context: ServiceContext
    compute: GatewayComputeService
    compute_states: RedisComputeStateRepository

    def unit_by_name(self, name: UnitName, *, workspace_id: str) -> ComputeUnitRecord:
        for unit in self.compute.list_units(workspace=workspace_id):
            if unit.name == name:
                return unit
        msg = f"unit not found: {name}"
        raise NotFoundError(msg)

    def unit_by_id(self, unit_id: str, *, workspace_id: str) -> ComputeUnitRecord:
        """Resolve the unit a public route addressed.

        Units are addressed by id so a request can never resolve a unit through
        a value that names a pool: the two share no shape.
        """
        for unit in self.compute.list_units(workspace=workspace_id):
            if unit.id == unit_id:
                return unit
        msg = f"unit not found: {unit_id}"
        raise NotFoundError(msg)

    def create_or_update_pool(
        self,
        config: projection.PoolConfig,
        *,
        workspace_id: str,
        pool: MachinePool = MachinePool(""),
    ) -> ComputeUnitRecord:
        if not config.name:
            msg = "pool name is required"
            raise InvalidInputError(msg)
        try:
            normalized = projection.normalize_unit_config(config)
            projection.compute_unit_from_config(config)
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc
        if normalized is None:
            msg = "pool config is required"
            raise InvalidInputError(msg)
        provider = (normalized.providers[0] if normalized.providers else "") or "agent"
        gpu_type = normalized.gpu[0] if normalized.gpu else ""
        return self.compute.create_unit(
            UnitName(normalized.name),
            pool=MachinePool(pool) if pool else None,
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
        unit: ComputeUnitRecord,
        *,
        workspace_id: str,
        config: projection.PoolConfig | None = None,
        owner_token_id: str = "gateway",
    ) -> PrivateUnitState:
        current = self.compute_states.get_unit_state(workspace_id, unit.capacity_owner_id)
        compute_config = config or pool_config_from_unit(unit)
        metadata: dict[str, JsonValue] = {
            **(current.metadata if current is not None else {}),
            "config": _JSON_OBJECT.validate_python(compute_config.model_dump(mode="json")),
            "created_by_token_id": (
                str((current.metadata if current else {}).get("created_by_token_id") or "")
                or owner_token_id
            ),
        }
        state = ComputeUnitState(
            workspace_id=workspace_id,
            name=unit.name,
            capacity_owner_id=unit.capacity_owner_id,
            provider=unit.provider,
            max_machines=max(unit.max_machines, 1),
            desired_machines=unit.max_machines,
            active_machines=self._unit_machine_count(unit, workspace_id=workspace_id),
            metadata=metadata,
        )
        self.compute_states.save_unit_state(state)
        return private_pool_from_compute_state(state)

    def _unit_machine_count(self, unit: ComputeUnitRecord, *, workspace_id: str) -> int:
        """How many machines this unit itself enrolled.

        Counting the unit's pool would count every unit feeding that pool, which
        would size one unit's capacity from another's machines.
        """
        with self.context.database.session() as session:
            return len(
                ComputeMachineEnrollmentRepository(session).list_for_unit(
                    workspace_id,
                    unit.capacity_owner_id,
                )
            )

    def private_unit_for_join_token(
        self,
        token_state: ComputeJoinTokenState | None,
    ) -> PrivateUnitState | None:
        """Resolve the unit a join credential was minted against.

        Keyed by capacity owner, not by the credential's pool name: that name is
        the pool the machine will join, and a pool may be fed by several units,
        so it identifies no single row to configure the agent from.
        """
        if token_state is None:
            return None
        unit = self.unit_by_capacity_owner(
            token_state.capacity_owner_id,
            workspace_id=token_state.workspace_id,
        )
        state = self.compute_states.get_unit_state(token_state.workspace_id, unit.capacity_owner_id)
        if state is not None:
            return private_pool_from_compute_state(state)
        return self.ensure_compute_pool_state(
            unit,
            workspace_id=token_state.workspace_id,
            owner_token_id=token_state.created_by_token_id or "gateway",
        )

    def unit_by_capacity_owner(
        self,
        capacity_owner_id: str,
        *,
        workspace_id: str,
    ) -> ComputeUnitRecord:
        for unit in self.compute.list_units(workspace=workspace_id):
            if unit.capacity_owner_id == capacity_owner_id:
                return unit
        msg = f"capacity owner not found: {capacity_owner_id}"
        raise NotFoundError(msg)

    def private_pool_by_name(
        self,
        unit_name: UnitName,
        *,
        workspace_id: str,
        owner_token_id: str = "gateway",
    ) -> PrivateUnitState:
        state = self.compute_states.get_unit_state(workspace_id, unit_name)
        if state is not None:
            return private_pool_from_compute_state(state)
        unit = self.unit_by_name(unit_name, workspace_id=workspace_id)
        return self.ensure_compute_pool_state(
            unit,
            workspace_id=workspace_id,
            owner_token_id=owner_token_id,
        )

    def create_unit_join_token(
        self,
        unit_name: UnitName,
        *,
        workspace_id: str,
        owner_token_id: str,
        ttl: str = "",
    ) -> JoinTokenCreationPlan:
        plan = self.plan_pool_join_token(
            unit_name,
            workspace_id=workspace_id,
            owner_token_id=owner_token_id,
            ttl=ttl,
        )
        current_time = utc_now()
        unit = self.unit_by_name(unit_name, workspace_id=workspace_id)
        with self.context.database.session() as session:
            credentials = ComputeJoinCredentialRepository(session)
            if not credentials.lock_unit(workspace_id, unit.capacity_owner_id):
                raise NotFoundError(f"pool not found: {unit_name}")
            previous = credentials.list_for_unit(
                workspace_id,
                unit.capacity_owner_id,
                for_update=True,
            )
            for credential in previous:
                if (
                    credential.status is ComputeCredentialStatus.Active
                    and credential.machine_id == ""
                ):
                    credentials.save(credential.revoke(now=current_time))
            durable = credentials.create(
                token_hash=plan.token_hash,
                workspace_id=workspace_id,
                capacity_owner_id=unit.capacity_owner_id,
                pool=unit.pool,
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

    def revoke_unit_join_token(self, unit_name: UnitName, *, workspace_id: str) -> None:
        current_time = utc_now()
        unit = self.unit_by_name(unit_name, workspace_id=workspace_id)
        with self.context.database.session() as session:
            credentials = ComputeJoinCredentialRepository(session)
            if not credentials.lock_unit(workspace_id, unit.capacity_owner_id):
                raise NotFoundError(f"pool not found: {unit_name}")
            records = credentials.list_for_unit(
                workspace_id,
                unit.capacity_owner_id,
                for_update=True,
            )
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
        unit_name: UnitName,
        *,
        workspace_id: str,
        owner_token_id: str,
        ttl: str = "",
    ) -> JoinTokenCreationPlan:
        try:
            unit = self.unit_by_name(unit_name, workspace_id=workspace_id)
            pool_state = self.ensure_compute_pool_state(
                unit,
                workspace_id=workspace_id,
                owner_token_id=owner_token_id,
            )
            return plan_join_token_creation(
                ComputePrincipal(
                    workspace_id=pool_state.workspace_id or "default",
                    owner_token_id=pool_state.created_by_token_id or "gateway",
                ),
                unit.pool,
                capacity_owner_id=unit.capacity_owner_id,
                ttl=ttl,
                max_uses=1,
            )
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc

    def save_compute_pool_config_update(
        self,
        workspace_id: str,
        pool_state: PrivateUnitState,
        config: projection.PoolConfig,
    ) -> None:
        current = self.compute_states.get_unit_state(workspace_id, pool_state.capacity_owner_id)
        metadata: dict[str, JsonValue] = {
            **(current.metadata if current is not None else {}),
            "config": _JSON_OBJECT.validate_python(config.model_dump(mode="json")),
            "created_by_token_id": pool_state.created_by_token_id or "gateway",
        }
        state = ComputeUnitState(
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
        self.compute_states.save_unit_state(state)

    def delete_compute_unit_state(self, capacity_owner_id: str, *, workspace_id: str) -> bool:
        return self.compute_states.delete_unit_state(workspace_id, capacity_owner_id)


__all__ = ["GatewayComputeService", "GatewayUnitStateCoordinator"]
