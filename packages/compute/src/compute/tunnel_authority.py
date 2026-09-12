from __future__ import annotations

import re
from dataclasses import dataclass
from hmac import compare_digest
from uuid import UUID

from database.repositories.compute import (
    ComputeMachineEnrollmentRecord,
    ComputeMachineEnrollmentRepository,
)
from database.repositories.identity import WorkspaceRepository
from database.repositories.orchestration import (
    ContainerRepository,
    MachineRepository,
    WorkerRepository,
)
from shared.compute_enrollment import ComputeMachineEnrollmentStatus
from shared.containers import LIVE_CONTAINER_STATUSES
from shared.errors import ConflictError, InvalidInputError, NotFoundError
from shared.http.agent_identity import AgentTunnelIdentity
from shared.routing import AgentBackendRoute, BackendRouteState
from shared.timestamps import utc_now
from sqlalchemy.orm import Session

from compute.state import RedisComputeStateRepository
from database import DatabaseClient


@dataclass(frozen=True, slots=True)
class AgentTunnelAuthority:
    database: DatabaseClient
    compute_states: RedisComputeStateRepository

    def bind_key(
        self,
        enrollment_id: str,
        workspace_id: str,
        credential_hash: str,
        public_key_sha256: str,
    ) -> AgentTunnelIdentity:
        if re.fullmatch(r"[0-9a-f]{64}", public_key_sha256) is None:
            raise InvalidInputError("Agent tunnel public key fingerprint must be SHA256")
        with self.database.session() as session:
            WorkspaceRepository(session).lock_active_owner(workspace_id)
            repository = ComputeMachineEnrollmentRepository(session)
            enrollment = repository.by_id(enrollment_id, workspace_id=workspace_id, for_update=True)
            if enrollment is None:
                raise NotFoundError("Agent enrollment is unavailable")
            if enrollment.status is not ComputeMachineEnrollmentStatus.Active or not compare_digest(
                enrollment.credential_hash, credential_hash
            ):
                raise ConflictError("Agent enrollment credential is no longer active")
            if enrollment.tunnel_public_key_sha256:
                if not compare_digest(enrollment.tunnel_public_key_sha256, public_key_sha256):
                    raise ConflictError("A different agent tunnel key requires reenrollment")
            else:
                repository.save(
                    enrollment.model_copy(
                        update={
                            "tunnel_public_key_sha256": public_key_sha256,
                            "updated_at": utc_now(),
                        }
                    )
                )
            return AgentTunnelIdentity(
                workspace_id=enrollment.workspace_id,
                enrollment_id=enrollment.id,
                credential_generation=enrollment.credential_generation,
            )

    def validate_agent(self, identity: AgentTunnelIdentity) -> None:
        with self.database.session() as session:
            self._active_enrollment(session, identity)

    def authorize_route(self, identity: AgentTunnelIdentity, route_id: str) -> AgentBackendRoute:
        with self.database.session() as session:
            enrollment = self._active_enrollment(session, identity)
            route = self.compute_states.get_agent_route_state(
                enrollment.workspace_id,
                enrollment.capacity_owner_id,
                enrollment.machine_id,
                route_id,
            )
            if route is None or (
                route.route_id != route_id
                or route.enrollment_id != enrollment.id
                or route.workspace_id != enrollment.workspace_id
                or route.capacity_owner_id != enrollment.capacity_owner_id
                or route.machine_id != enrollment.machine_id
                or route.pool != enrollment.pool
            ):
                raise NotFoundError("Agent backend route is unavailable")
            if route.state is not BackendRouteState.Ready:
                raise ConflictError("Agent backend route is not ready")
            try:
                UUID(route.worker_id)
                UUID(route.container_id)
            except ValueError as exc:
                raise NotFoundError("Agent backend destination is unavailable") from exc
            machine = MachineRepository(session).get(
                enrollment.machine_id, workspace_id=enrollment.workspace_id
            )
            worker = WorkerRepository(session).get(
                route.worker_id, workspace_id=enrollment.workspace_id
            )
            if (
                machine is None
                or machine.capacity_owner_id != enrollment.capacity_owner_id
                or worker is None
                or worker.machine_id != enrollment.machine_id
                or worker.pool != enrollment.pool
            ):
                raise NotFoundError("Agent backend worker is unavailable")
            container = ContainerRepository(session).get_across_workspaces(route.container_id)
            if (
                container is None
                or container.runtime_worker_id != route.worker_id
                or container.runtime_machine_id != enrollment.machine_id
                or container.status not in LIVE_CONTAINER_STATUSES
            ):
                raise NotFoundError("Agent backend container is unavailable")
            return route

    @staticmethod
    def _active_enrollment(
        session: Session, identity: AgentTunnelIdentity
    ) -> ComputeMachineEnrollmentRecord:
        enrollment = ComputeMachineEnrollmentRepository(session).by_id(
            identity.enrollment_id, workspace_id=identity.workspace_id
        )
        if enrollment is None:
            raise NotFoundError("Agent enrollment is unavailable")
        if (
            enrollment.status is not ComputeMachineEnrollmentStatus.Active
            or enrollment.credential_generation != identity.credential_generation
            or not enrollment.tunnel_public_key_sha256
        ):
            raise ConflictError("Agent tunnel identity is no longer active")
        return enrollment


__all__ = ["AgentTunnelAuthority"]
