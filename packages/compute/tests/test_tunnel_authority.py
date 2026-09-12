from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from uuid import uuid4

import pytest
from compute.state import RedisComputeStateRepository
from compute.tunnel_authority import AgentTunnelAuthority
from control.service import ControlPlaneService
from database.context import ServiceContext
from database.repositories.compute import (
    ComputeMachineEnrollmentCreate,
    ComputeMachineEnrollmentRecord,
    ComputeMachineEnrollmentRepository,
)
from database.repositories.orchestration import (
    ContainerRepository,
    MachineRepository,
    WorkerRepository,
)
from shared.compute_enrollment import ComputeMachineEnrollmentStatus
from shared.compute_fleet import Machine, ResourceStatus, Worker
from shared.compute_policy import MachinePool
from shared.containers import ContainerRecord, ContainerStatus
from shared.errors import ConflictError, NotFoundError
from shared.http.agent_identity import AgentTunnelIdentity
from shared.routing import AgentBackendRoute, BackendRouteKind, BackendRouteState
from shared.timestamps import utc_now
from tests.real_redis import RealRedisActors
from tests.workspaces import owned_workspace, workspace_owner_user_id


def test_tunnel_key_binding_serializes_and_fences_reenrollment_and_revocation(
    committed_service_context: ServiceContext,
    real_redis_actors: RealRedisActors,
) -> None:
    context = committed_service_context
    enrollment = _enroll(context)
    authority = AgentTunnelAuthority(
        context.database, RedisComputeStateRepository(real_redis_actors.client())
    )
    identity = AgentTunnelIdentity(
        workspace_id=enrollment.workspace_id,
        enrollment_id=enrollment.id,
        credential_generation=1,
    )
    with pytest.raises(ConflictError):
        authority.validate_agent(identity)
    keys = ("a" * 64, "b" * 64)
    accepted: list[str] = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = [
            executor.submit(
                authority.bind_key,
                enrollment.id,
                enrollment.workspace_id,
                enrollment.credential_hash,
                key,
            )
            for key in keys
        ]
        for key, claim in zip(keys, claims, strict=True):
            try:
                assert claim.result() == identity
            except ConflictError:
                continue
            accepted.append(key)
    assert len(accepted) == 1
    key = accepted[0]
    assert (
        authority.bind_key(enrollment.id, enrollment.workspace_id, enrollment.credential_hash, key)
        == identity
    )
    authority.validate_agent(identity)
    with pytest.raises(ConflictError):
        authority.bind_key(enrollment.id, enrollment.workspace_id, "c" * 64, key)
    with pytest.raises(NotFoundError):
        authority.validate_agent(identity.model_copy(update={"workspace_id": str(uuid4())}))

    snapshot = ComputeMachineEnrollmentCreate(
        user_id=enrollment.user_id,
        workspace_id=enrollment.workspace_id,
        capacity_owner_id=enrollment.capacity_owner_id,
        pool=enrollment.pool,
        machine_id=enrollment.machine_id,
        machine_fingerprint_hash=enrollment.machine_fingerprint_hash,
        credential_hash=enrollment.credential_hash,
        last_join_at=utc_now(),
    )
    with context.database.session() as session:
        repository = ComputeMachineEnrollmentRepository(session)
        current = repository.by_id(enrollment.id, workspace_id=enrollment.workspace_id)
        assert current is not None
        current = repository.save(snapshot.update_record(current, updated_at=utc_now()))
        assert current.tunnel_public_key_sha256 == key
        snapshot = snapshot.model_copy(
            update={"credential_generation": 2, "credential_hash": "d" * 64}
        )
        current = repository.save(snapshot.update_record(current, updated_at=utc_now()))
        assert current.tunnel_public_key_sha256 == ""
    with pytest.raises(ConflictError):
        authority.validate_agent(identity)
    replacement = authority.bind_key(enrollment.id, enrollment.workspace_id, "d" * 64, "e" * 64)
    assert replacement.credential_generation == 2
    authority.validate_agent(replacement)
    with context.database.session() as session:
        repository = ComputeMachineEnrollmentRepository(session)
        current = repository.by_id(enrollment.id, workspace_id=enrollment.workspace_id)
        assert current is not None
        repository.save(
            current.model_copy(update={"status": ComputeMachineEnrollmentStatus.Revoked})
        )
    with pytest.raises(ConflictError):
        authority.validate_agent(replacement)
    with pytest.raises(ConflictError):
        authority.bind_key(enrollment.id, enrollment.workspace_id, "d" * 64, "e" * 64)


def test_tunnel_routes_require_live_destination_assignment_within_enrollment_scope(
    service_context: ServiceContext,
    real_redis_actors: RealRedisActors,
) -> None:
    enrollment = _enroll(service_context)
    states = RedisComputeStateRepository(real_redis_actors.client())
    authority = AgentTunnelAuthority(service_context.database, states)
    identity = authority.bind_key(
        enrollment.id, enrollment.workspace_id, enrollment.credential_hash, "a" * 64
    )
    worker_id, foreign_worker_id, foreign_machine_id = (str(uuid4()) for _ in range(3))
    workload_workspace = owned_workspace(ControlPlaneService(service_context), "tunnel-workload")
    with service_context.database.session() as session:
        MachineRepository(session).upsert(
            Machine(
                id=foreign_machine_id, capacity_owner_id=str(uuid4()), status=ResourceStatus.Running
            ),
            workspace_id=enrollment.workspace_id,
        )
        for worker, machine in (
            (worker_id, enrollment.machine_id),
            (foreign_worker_id, foreign_machine_id),
        ):
            WorkerRepository(session).upsert(
                Worker(
                    id=worker,
                    machine_id=machine,
                    pool=enrollment.pool,
                    status=ResourceStatus.Created,
                ),
                workspace_id=enrollment.workspace_id,
            )
        container = ContainerRepository(session).upsert(
            ContainerRecord(
                id=str(uuid4()),
                name="tunnel-workload",
                workspace_id=workload_workspace.id,
                image="",
                command=[],
                runtime_machine_id=enrollment.machine_id,
                runtime_worker_id=worker_id,
                status=ContainerStatus.Running,
            )
        )
    route = AgentBackendRoute(
        route_id="container-route",
        enrollment_id=enrollment.id,
        workspace_id=enrollment.workspace_id,
        capacity_owner_id=enrollment.capacity_owner_id,
        pool=enrollment.pool,
        machine_id=enrollment.machine_id,
        worker_id=worker_id,
        container_id=container.id,
        kind=BackendRouteKind.Container,
        state=BackendRouteState.Ready,
        local_target="127.0.0.1:8080",
        port=8080,
    )
    states.save_agent_route_state(route)
    assert authority.authorize_route(identity, route.route_id) == route
    worker_route = route.model_copy(
        update={"route_id": "worker-route", "kind": BackendRouteKind.Worker, "port": 0}
    )
    states.save_agent_route_state(worker_route)
    assert authority.authorize_route(identity, worker_route.route_id) == worker_route
    states.save_agent_route_state(route.model_copy(update={"state": BackendRouteState.Opening}))
    with pytest.raises(ConflictError):
        authority.authorize_route(identity, route.route_id)
    states.save_agent_route_state(route.model_copy(update={"worker_id": foreign_worker_id}))
    with pytest.raises(NotFoundError):
        authority.authorize_route(identity, route.route_id)
    states.save_agent_route_state(
        route.model_copy(update={"route_id": "foreign-route", "machine_id": foreign_machine_id})
    )
    with pytest.raises(NotFoundError):
        authority.authorize_route(identity, "foreign-route")
    with service_context.database.session() as session:
        foreign_container = ContainerRepository(session).upsert(
            container.model_copy(
                update={
                    "id": str(uuid4()),
                    "runtime_worker_id": foreign_worker_id,
                    "runtime_machine_id": foreign_machine_id,
                }
            )
        )
    states.save_agent_route_state(route.model_copy(update={"container_id": foreign_container.id}))
    with pytest.raises(NotFoundError):
        authority.authorize_route(identity, route.route_id)
    with service_context.database.session() as session:
        ContainerRepository(session).upsert(
            container.model_copy(update={"status": ContainerStatus.Stopped})
        )
    states.save_agent_route_state(route)
    with pytest.raises(NotFoundError):
        authority.authorize_route(identity, route.route_id)


def _enroll(context: ServiceContext) -> ComputeMachineEnrollmentRecord:
    with context.database.session() as session:
        workspace_id = context.default_workspace_id(session)
    user_id = workspace_owner_user_id(context, workspace_id)
    machine_id, capacity_owner_id = str(uuid4()), str(uuid4())
    with context.database.session() as session:
        MachineRepository(session).upsert(
            Machine(
                id=machine_id, capacity_owner_id=capacity_owner_id, status=ResourceStatus.Created
            ),
            workspace_id=workspace_id,
        )
        return ComputeMachineEnrollmentRepository(session).create(
            ComputeMachineEnrollmentCreate(
                user_id=user_id,
                workspace_id=workspace_id,
                capacity_owner_id=capacity_owner_id,
                pool=MachinePool("lazycloud"),
                machine_id=machine_id,
                machine_fingerprint_hash=sha256(machine_id.encode()).hexdigest(),
                credential_hash=sha256(uuid4().bytes).hexdigest(),
                last_join_at=utc_now(),
            )
        )
