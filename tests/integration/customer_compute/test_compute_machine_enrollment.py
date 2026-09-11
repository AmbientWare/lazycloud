from __future__ import annotations

import base64
import hashlib
import shlex
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from api.server.services import ApiServices
from api.server.workspace_deletion import WorkspaceDeletionService
from compute.agent_control import agent_machine_worker_id, hash_compute_token
from compute.state import RedisComputeStateRepository
from control.service import ControlPlaneService
from database.repositories.compute import (
    PRIMARY_WIREGUARD_GATEWAY_ID,
    ComputeJoinCredentialRepository,
    ComputeMachineEnrollmentRepository,
    WireGuardGatewayRepository,
    WireGuardPeerRepository,
)
from database.repositories.orchestration import MachineRepository, WorkerRepository
from database.repositories.source_cache import SourceCacheCleanupRepository
from gateway.http import (
    AgentMetricSnapshot,
    AgentTelemetryRequest,
    JoinAgentRequest,
    LeaveAgentRequest,
    RegisterAgentPrivateNetworkRequest,
    StreamAgentRequest,
)
from gateway.service import SELF_HOSTED_FLEET_POOL_NAME, GatewayControlService
from observability.usage import UsageService
from operations.management import ManagementService
from scheduler.fleet import SchedulerContainerStatus
from scheduler.pool_state import SchedulerPoolStateService
from scheduler.state import (
    RedisSchedulerContainerRepository,
    RedisSchedulerWorkerRepository,
    RedisWorkerPoolStateRepository,
    SchedulerContainerState,
)
from shared.compute_enrollment import (
    AgentCapacityState,
    AgentWorkerSlotStatus,
    ComputeCredentialStatus,
    ComputePreflightCheck,
    MachineReadinessPhase,
    PreflightSeverity,
    PrivateNetworkEnrollmentPhase,
    WireGuardGateway,
)
from shared.compute_fleet import ResourceStatus
from shared.compute_policy import (
    MachinePool,
    UnitName,
)
from shared.errors import ConflictError, InvalidInputError
from shared.http.compute import MachineJoinCommandRequest, UnitMachineResponse
from shared.http.gateway import AgentCapacityInterruptionRequest
from shared.identity import TokenKind, WorkspaceStatus
from shared.scheduling import SchedulerWorkerRecord, SchedulerWorkerStatus
from shared.timestamps import utc_now
from shared.usage import UsageBillingOwner
from tests.real_redis import RealRedisActors
from tests.releases import select_worker_release
from tests.workspaces import administrator_credential, owned_workspace, workspace_owner_user_id
from worker.repository_payloads import WorkerRepositoryPrincipal
from worker_repository.source_cache import WorkerSourceCacheService


class _ProbeConnection:
    def close(self) -> None:
        return None


class _PrivateNetworkConnector:
    def __init__(self) -> None:
        self.reachable = True

    def connect(self, _address: str, _timeout_seconds: float) -> _ProbeConnection:
        if not self.reachable:
            raise TimeoutError("route proxy is unreachable")
        return _ProbeConnection()


def _gateway(
    services: ApiServices,
    *,
    private_network_connector: _PrivateNetworkConnector | None = None,
) -> GatewayControlService:
    _publish_wireguard_gateway(services)
    return replace(
        services.gateway_service,
        private_network_connector=private_network_connector or _PrivateNetworkConnector(),
    )


def _wireguard_public_key(identity: str) -> str:
    return base64.b64encode(hashlib.sha256(identity.encode()).digest()).decode()


def _publish_wireguard_gateway(services: ApiServices) -> None:
    with services.context.database.session() as session:
        WireGuardGatewayRepository(session).save(
            WireGuardGateway(
                id=PRIMARY_WIREGUARD_GATEWAY_ID,
                public_key=_wireguard_public_key("test-gateway"),
                endpoint="wireguard.test:51820",
                updated_at=utc_now(),
            )
        )


def _create_join_token(
    gateway: GatewayControlService,
    pool: MachinePool,
    workspace_id: str,
):
    # The credential names the account the machine will belong to, so the workspace
    # has to have the owner row production writes with it.
    workspace_owner_user_id(gateway.services.context, workspace_id)
    return gateway.unit_state_coordinator.create_unit_join_token(
        gateway.unit_state_coordinator.unit_by_name(UnitName(pool), workspace_id=workspace_id),
        workspace_id=workspace_id,
        owner_token_id="local-cli",
    )


def _pool_machines(
    gateway: GatewayControlService,
    pool: MachinePool,
    workspace_id: str,
) -> list[UnitMachineResponse]:
    return [machine for machine in gateway.machine_views(workspace_id) if machine.pool == pool]


def _bind_private_network(
    gateway: GatewayControlService,
    workspace_id: str,
    agent_token: str,
    machine_id: str,
) -> str:
    binding = gateway.register_agent_private_network(
        RegisterAgentPrivateNetworkRequest(
            agent_token=agent_token,
            public_key=_wireguard_public_key(machine_id),
        )
    )
    with gateway.services.context.database.session() as session:
        enrollment = ComputeMachineEnrollmentRepository(session).by_machine(
            workspace_id,
            machine_id,
        )
        assert enrollment is not None
        peers = WireGuardPeerRepository(session)
        peer = peers.by_enrollment(enrollment.id, for_update=True)
        assert peer is not None
        peers.save(peer.model_copy(update={"last_handshake_at": utc_now()}))
    return binding.peer_id


def _join_request(token: str, *, fingerprint: str = "host-fingerprint") -> JoinAgentRequest:
    return JoinAgentRequest(
        join_token=token,
        machine_fingerprint=fingerprint,
        hostname="customer-host",
        os="linux",
        arch="amd64",
        cpu_count=8,
        memory_mb=16_384,
        preflight=[
            ComputePreflightCheck(
                name="container-runtime",
                ok=True,
                severity=PreflightSeverity.Error,
                remediation="Install and start Docker, then retry enrollment.",
            )
        ],
    )


def _workspace_id(services: ApiServices) -> str:
    with services.context.database.session() as session:
        return services.context.default_workspace_id(session)


def test_machine_enrollment_is_durable_rotatable_and_secret_free(
    isolated_services: ApiServices,
) -> None:
    workspace_id = _workspace_id(isolated_services)
    isolated_services.compute.create_unit(
        UnitName("customer-machines"),
        provider="agent",
        workspace=workspace_id,
    )
    gateway = _gateway(isolated_services)
    bootstrap = _create_join_token(gateway, MachinePool("customer-machines"), workspace_id)

    joined = gateway.join_agent(_join_request(bootstrap.token))
    _bind_private_network(gateway, workspace_id, joined.agent_token, joined.machine_id)

    assert UUID(joined.machine_id)
    assert joined.bootstrap is not None
    view = _pool_machines(gateway, MachinePool("customer-machines"), workspace_id)[0]
    assert view.readiness_phase is MachineReadinessPhase.Joining
    assert not view.schedulable
    assert {
        "registration_token",
        "user_data",
    }.isdisjoint(view.model_dump(mode="json"))

    with isolated_services.context.database.session() as session:
        credential = ComputeJoinCredentialRepository(session).get_by_hash(
            hash_compute_token(bootstrap.token)
        )
        enrollment = ComputeMachineEnrollmentRepository(session).by_machine(
            workspace_id,
            joined.machine_id,
            pool=MachinePool("customer-machines"),
        )
        worker = WorkerRepository(session).get_across_workspaces(
            agent_machine_worker_id(joined.machine_id)
        )
    assert credential is not None
    assert credential.created_by_token_id is None
    assert credential.max_uses == 1
    assert credential.use_count == 1
    assert enrollment is not None
    assert joined.credential_id == enrollment.id
    assert joined.credential_generation == enrollment.credential_generation
    assert enrollment.credential_hash == hash_compute_token(joined.agent_token)
    assert enrollment.machine_fingerprint_hash != "host-fingerprint"
    assert worker is not None
    assert worker.machine_id == joined.machine_id
    assert worker.pool == "customer-machines"

    heartbeat = gateway.stream_agent(StreamAgentRequest(agent_token=joined.agent_token))
    assert heartbeat.ok
    ready = _pool_machines(gateway, MachinePool("customer-machines"), workspace_id)[0]
    assert ready.readiness_phase is MachineReadinessPhase.Ready
    assert ready.schedulable

    gateway.compute_states.delete_agent_token_state(hash_compute_token(joined.agent_token))
    assert gateway.stream_agent(StreamAgentRequest(agent_token=joined.agent_token)).ok

    rejoined = gateway.join_agent(_join_request(bootstrap.token))
    assert rejoined.machine_id == joined.machine_id
    assert rejoined.agent_token != joined.agent_token
    assert not gateway.stream_agent(StreamAgentRequest(agent_token=joined.agent_token)).ok
    assert gateway.stream_agent(StreamAgentRequest(agent_token=rejoined.agent_token)).ok

    with isolated_services.context.database.session() as session:
        rotated = ComputeMachineEnrollmentRepository(session).by_machine(
            workspace_id,
            rejoined.machine_id,
            pool=MachinePool("customer-machines"),
        )
        credential = ComputeJoinCredentialRepository(session).get_by_hash(
            hash_compute_token(bootstrap.token)
        )
    assert rotated is not None
    assert rotated.credential_generation == 2
    assert credential is not None
    assert credential.use_count == 1

    with pytest.raises(ConflictError, match="machine limit"):
        gateway.join_agent(_join_request(bootstrap.token, fingerprint="other-host"))


def test_worker_image_update_pulls_then_switches_after_started_work_finishes(
    isolated_services: ApiServices,
) -> None:
    services = isolated_services
    workspace_id = _workspace_id(services)
    pool = MachinePool("worker-image-update")
    unit = services.compute.create_unit(UnitName(pool), provider="agent", workspace=workspace_id)
    gateway = _gateway(services)
    select_worker_release("registry.test/worker@sha256:old")
    assert isinstance(gateway.scheduler_workers, RedisSchedulerWorkerRepository)
    assert isinstance(gateway.scheduler_containers, RedisSchedulerContainerRepository)
    scheduler_workers = gateway.scheduler_workers
    scheduler_containers = gateway.scheduler_containers
    bootstrap = _create_join_token(gateway, pool, workspace_id)
    joined = gateway.join_agent(_join_request(bootstrap.token))
    _bind_private_network(gateway, workspace_id, joined.agent_token, joined.machine_id)
    worker_id = agent_machine_worker_id(joined.machine_id)
    scheduler_workers.add_worker(
        SchedulerWorkerRecord(
            worker_id=worker_id,
            pool=pool,
            capacity_owner_id=unit.capacity_owner_id,
            workspace_id=workspace_id,
            machine_id=joined.machine_id,
            status=SchedulerWorkerStatus.Available,
            billing_owner=UsageBillingOwner.SelfHosted,
        )
    )
    current_image = {worker_id: "registry.test/worker@sha256:old"}
    assert (
        gateway.stream_agent(
            StreamAgentRequest(agent_token=joined.agent_token, active_worker_images=current_image)
        )
        .slots[0]
        .status
        is AgentWorkerSlotStatus.Active
    )

    container = SchedulerContainerState(
        container_id="running-during-worker-image-update",
        workspace_id=workspace_id,
        stub_id="running-workload",
        worker_id=worker_id,
        status=SchedulerContainerStatus.Running,
    )
    scheduler_containers.set_container_state(container)
    pool_states = SchedulerPoolStateService(
        scheduler_workers,
        scheduler_containers,
        gateway.scheduler_pool_state_repository,
    )
    pool_states.refresh()
    select_worker_release("registry.test/worker@sha256:new")
    held = gateway.stream_agent(
        StreamAgentRequest(agent_token=joined.agent_token, active_worker_images=current_image)
    )
    assert held.slots[0].status is AgentWorkerSlotStatus.Active
    preparing = scheduler_workers.get_worker(worker_id)
    assert preparing is not None and preparing.status is SchedulerWorkerStatus.Available

    draining = gateway.stream_agent(
        StreamAgentRequest(
            agent_token=joined.agent_token,
            active_worker_images=current_image,
            prepared_worker_images=["registry.test/worker@sha256:new"],
        )
    )

    assert draining.slots[0].worker_image == "registry.test/worker@sha256:new"
    assert draining.slots[0].status is AgentWorkerSlotStatus.Draining
    drained_worker = scheduler_workers.get_worker(worker_id)
    assert drained_worker is not None
    assert drained_worker.status is SchedulerWorkerStatus.Draining

    scheduler_containers.update_container_status(
        container.container_id,
        SchedulerContainerStatus.Complete,
    )
    switch = gateway.stream_agent(
        StreamAgentRequest(
            agent_token=joined.agent_token,
            active_worker_images=current_image,
            prepared_worker_images=["registry.test/worker@sha256:new"],
        )
    )
    assert switch.slots[0].status is AgentWorkerSlotStatus.Pending
    assert switch.slots[0].worker_id == worker_id
    assert switch.slots[0].machine_id == joined.machine_id


def test_private_network_registration_requires_a_fresh_handshake(
    isolated_services: ApiServices,
) -> None:
    workspace_id = _workspace_id(isolated_services)
    isolated_services.compute.create_unit(
        UnitName("fresh-private-network"),
        provider="agent",
        workspace=workspace_id,
    )
    connector = _PrivateNetworkConnector()
    gateway = _gateway(isolated_services, private_network_connector=connector)
    bootstrap = _create_join_token(gateway, MachinePool("fresh-private-network"), workspace_id)
    joined = gateway.join_agent(_join_request(bootstrap.token))
    public_key = _wireguard_public_key(joined.machine_id)

    first = gateway.register_agent_private_network(
        RegisterAgentPrivateNetworkRequest(
            agent_token=joined.agent_token,
            public_key=public_key,
        )
    )
    with isolated_services.context.database.session() as session:
        enrollment = ComputeMachineEnrollmentRepository(session).by_machine(
            workspace_id,
            joined.machine_id,
            for_update=True,
        )
        assert enrollment is not None
        peers = WireGuardPeerRepository(session)
        peer = peers.by_enrollment(enrollment.id, for_update=True)
        assert peer is not None
        peers.save(peer.model_copy(update={"last_handshake_at": utc_now()}))
    assert gateway.stream_agent(StreamAgentRequest(agent_token=joined.agent_token)).ok

    second = gateway.register_agent_private_network(
        RegisterAgentPrivateNetworkRequest(
            agent_token=joined.agent_token,
            public_key=public_key,
        )
    )

    assert second.generation == first.generation + 1
    with isolated_services.context.database.session() as session:
        enrollment = ComputeMachineEnrollmentRepository(session).by_machine(
            workspace_id,
            joined.machine_id,
        )
        assert enrollment is not None
        peer = WireGuardPeerRepository(session).by_enrollment(enrollment.id)
    assert enrollment.network_phase is PrivateNetworkEnrollmentPhase.AwaitingHandshake
    assert enrollment.network_verified_at is None
    assert not enrollment.schedulable
    assert peer is not None
    assert peer.last_handshake_at is None
    rejected = gateway.stream_agent(StreamAgentRequest(agent_token=joined.agent_token))
    assert not rejected.ok
    assert rejected.retryable
    assert rejected.err_msg == "agent WireGuard handshake has not been observed"

    with isolated_services.context.database.session() as session:
        peers = WireGuardPeerRepository(session)
        current = peers.by_enrollment(enrollment.id, for_update=True)
        assert current is not None
        peers.save(current.model_copy(update={"last_handshake_at": utc_now()}))
    connector.reachable = False
    rejected = gateway.stream_agent(StreamAgentRequest(agent_token=joined.agent_token))
    assert not rejected.ok
    assert rejected.retryable
    assert "could not reach TCP 29443" in rejected.err_msg
    [machine] = _pool_machines(
        gateway,
        MachinePool("fresh-private-network"),
        workspace_id,
    )
    assert machine.readiness_phase is MachineReadinessPhase.Blocked
    assert machine.readiness_message in machine.remediation
    assert "100.96.0.0/24" in machine.readiness_message

    connector.reachable = True
    assert gateway.stream_agent(StreamAgentRequest(agent_token=joined.agent_token)).ok
    with isolated_services.context.database.session() as session:
        connected = ComputeMachineEnrollmentRepository(session).by_machine(
            workspace_id,
            joined.machine_id,
        )
    assert connected is not None
    assert connected.network_phase is PrivateNetworkEnrollmentPhase.Connected
    assert connected.network_verified_at is not None
    assert connected.network_failure_detail == ""


def test_capacity_interruption_is_session_fenced_durable_and_heartbeat_safe(
    isolated_services: ApiServices,
) -> None:
    workspace_id = _workspace_id(isolated_services)
    isolated_services.compute.create_unit(
        UnitName("preemptible-machines"),
        provider="agent",
        workspace=workspace_id,
    )
    gateway = _gateway(isolated_services)
    bootstrap = _create_join_token(gateway, MachinePool("preemptible-machines"), workspace_id)
    joined = gateway.join_agent(_join_request(bootstrap.token))
    _bind_private_network(gateway, workspace_id, joined.agent_token, joined.machine_id)
    assert gateway.stream_agent(StreamAgentRequest(agent_token=joined.agent_token)).ok
    observed_at = datetime(2026, 7, 21, tzinfo=UTC)
    request = AgentCapacityInterruptionRequest(
        agent_token=joined.agent_token,
        machine_id=joined.machine_id,
        credential_id=joined.credential_id,
        credential_generation=joined.credential_generation,
        state=AgentCapacityState.Preempting,
        reason="provider interruption notice",
        observed_at=observed_at,
    )

    recorded = gateway.record_agent_capacity_interruption(request)
    repeated = gateway.record_agent_capacity_interruption(request)
    heartbeat = gateway.stream_agent(StreamAgentRequest(agent_token=joined.agent_token))

    assert recorded.changed
    assert not repeated.changed
    assert heartbeat.ok
    assert heartbeat.capacity_state is AgentCapacityState.Preempting
    with isolated_services.context.database.session() as session:
        enrollment = ComputeMachineEnrollmentRepository(session).by_machine(
            workspace_id,
            joined.machine_id,
            pool=MachinePool("preemptible-machines"),
        )
    assert enrollment is not None
    assert not enrollment.schedulable
    assert enrollment.capacity_state is AgentCapacityState.Preempting
    machine = _pool_machines(gateway, MachinePool("preemptible-machines"), workspace_id)[0]
    assert not machine.schedulable
    assert machine.capacity_state is AgentCapacityState.Preempting

    rejoined = gateway.join_agent(_join_request(bootstrap.token))
    with pytest.raises(ConflictError, match="session fence is stale"):
        gateway.record_agent_capacity_interruption(
            request.model_copy(
                update={
                    "agent_token": rejoined.agent_token,
                    "credential_id": rejoined.credential_id,
                }
            )
        )


def test_agent_leave_cleans_up_and_public_delete_requires_host_decommission(
    isolated_services: ApiServices,
) -> None:
    workspace_id = _workspace_id(isolated_services)
    isolated_services.compute.create_unit(
        UnitName("cleanup-machines"),
        provider="agent",
        workspace=workspace_id,
    )
    gateway = _gateway(isolated_services)
    first_token = _create_join_token(gateway, MachinePool("cleanup-machines"), workspace_id)
    first = gateway.join_agent(_join_request(first_token.token, fingerprint="first-host"))
    first_peer_id = _bind_private_network(
        gateway,
        workspace_id,
        first.agent_token,
        first.machine_id,
    )

    left = gateway.leave_agent(LeaveAgentRequest(agent_token=first.agent_token))
    assert left.machine_id == first.machine_id
    assert _pool_machines(gateway, MachinePool("cleanup-machines"), workspace_id) == []
    assert not gateway.stream_agent(StreamAgentRequest(agent_token=first.agent_token)).ok
    with isolated_services.context.database.session() as session:
        assert (
            WireGuardPeerRepository(session).records.get(
                first_peer_id,
                workspace_id=workspace_id,
            )
            is None
        )
        assert MachineRepository(session).get_across_workspaces(first.machine_id) is None
        assert (
            WorkerRepository(session).get_across_workspaces(
                agent_machine_worker_id(first.machine_id)
            )
            is None
        )
        assert (
            ComputeMachineEnrollmentRepository(session).by_machine(
                workspace_id,
                first.machine_id,
                pool=MachinePool("cleanup-machines"),
            )
            is None
        )
        credential = ComputeJoinCredentialRepository(session).get_by_hash(
            hash_compute_token(first_token.token)
        )
    assert credential is not None
    assert credential.status is ComputeCredentialStatus.Revoked

    second_token = _create_join_token(gateway, MachinePool("cleanup-machines"), workspace_id)
    second = gateway.join_agent(_join_request(second_token.token, fingerprint="second-host"))
    with pytest.raises(ConflictError, match="lazycloud-agent leave"):
        gateway.delete_machine(
            second.machine_id,
            workspace_id=workspace_id,
            pool=MachinePool("cleanup-machines"),
        )
    assert (
        gateway.compute_states.get_agent_token_state(hash_compute_token(second.agent_token))
        is not None
    )
    with isolated_services.context.database.session() as session:
        assert MachineRepository(session).get_across_workspaces(second.machine_id) is not None
        assert (
            WorkerRepository(session).get_across_workspaces(
                agent_machine_worker_id(second.machine_id)
            )
            is not None
        )
        assert (
            ComputeMachineEnrollmentRepository(session).by_machine(
                workspace_id,
                second.machine_id,
                pool=MachinePool("cleanup-machines"),
            )
            is not None
        )


def test_agent_leave_requires_current_machine_cache_destruction_session(
    isolated_services: ApiServices,
) -> None:
    workspace_id = _workspace_id(isolated_services)
    isolated_services.compute.create_unit(
        UnitName("cache-decommission"),
        provider="agent",
        workspace=workspace_id,
    )
    gateway = _gateway(isolated_services)
    bootstrap = _create_join_token(gateway, MachinePool("cache-decommission"), workspace_id)
    joined = gateway.join_agent(_join_request(bootstrap.token))
    worker_id = agent_machine_worker_id(joined.machine_id)
    generation = WorkerSourceCacheService(isolated_services.context).register(
        principal=WorkerRepositoryPrincipal(
            workspace_id=workspace_id,
            worker_id=worker_id,
            token_kind=TokenKind.WorkerPrivate,
        ),
        worker_id=worker_id,
        generation_id=str(uuid4()),
        storage_id=f"machine:{joined.machine_id}",
    )

    with pytest.raises(ConflictError, match="must be destroyed"):
        gateway.leave_agent(
            LeaveAgentRequest(
                agent_token=joined.agent_token,
                machine_id=joined.machine_id,
            )
        )
    with pytest.raises(ConflictError, match="current machine session"):
        gateway.leave_agent(
            LeaveAgentRequest(
                agent_token=joined.agent_token,
                machine_id=joined.machine_id,
                cache_generation_id=generation.id,
                cache_session_fence=generation.session_fence + 1,
            )
        )

    left = gateway.leave_agent(
        LeaveAgentRequest(
            agent_token=joined.agent_token,
            machine_id=joined.machine_id,
            cache_generation_id=generation.id,
            cache_session_fence=generation.session_fence,
        )
    )

    assert left.machine_id == joined.machine_id
    with isolated_services.context.database.session() as session:
        retired = SourceCacheCleanupRepository(session).get_generation(generation.id)
        enrollment = ComputeMachineEnrollmentRepository(session).by_machine(
            workspace_id,
            joined.machine_id,
            pool=MachinePool("cache-decommission"),
        )
    assert retired is not None
    assert retired.storage_destroyed_at is not None
    assert enrollment is None


def test_pool_delete_requires_host_decommission_without_mutating_ownership(
    isolated_services: ApiServices,
) -> None:
    workspace_id = _workspace_id(isolated_services)
    unit = isolated_services.compute.create_unit(
        UnitName("deleted-machine-pool"),
        provider="agent",
        workspace=workspace_id,
    )
    gateway = _gateway(isolated_services)
    bootstrap = _create_join_token(gateway, MachinePool("deleted-machine-pool"), workspace_id)
    joined = gateway.join_agent(_join_request(bootstrap.token))
    join_token_hash = hash_compute_token(bootstrap.token)
    agent_token_hash = hash_compute_token(joined.agent_token)
    assert gateway.compute_states.get_join_token_state(join_token_hash) is not None
    assert gateway.compute_states.get_agent_token_state(agent_token_hash) is not None

    with pytest.raises(ConflictError, match="lazycloud-agent leave"):
        gateway.delete_unit(unit.id, workspace_id=workspace_id)

    assert gateway.compute_states.get_unit_state(workspace_id, unit.capacity_owner_id) is not None
    assert gateway.compute_states.get_join_token_state(join_token_hash) is not None
    assert gateway.compute_states.get_agent_token_state(agent_token_hash) is not None
    with isolated_services.context.database.session() as session:
        assert MachineRepository(session).get_across_workspaces(joined.machine_id) is not None
        assert (
            WorkerRepository(session).get_across_workspaces(
                agent_machine_worker_id(joined.machine_id)
            )
            is not None
        )
        assert (
            ComputeMachineEnrollmentRepository(session).list_for_unit(
                workspace_id,
                unit.capacity_owner_id,
            )
            != []
        )
        assert (
            ComputeJoinCredentialRepository(session).list_for_unit(
                workspace_id,
                unit.capacity_owner_id,
            )
            != []
        )


def test_workspace_deletion_preflight_preserves_enrolled_self_hosted_ownership(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    services = isolated_services
    control = ControlPlaneService(services.context)
    owned_workspace(control, "default")
    _raw_token, audit_actor = administrator_credential(
        isolated_services.context, "workspace-delete-admin"
    )
    workspace = owned_workspace(control, "enrolled-customer")
    unit = services.compute.create_unit(
        UnitName("workspace-machine-pool"),
        provider="agent",
        workspace=workspace.id,
    )
    gateway = replace(
        services.gateway_service,
        compute_state=RedisComputeStateRepository(redis),
        scheduler_workers=RedisSchedulerWorkerRepository(redis),
        scheduler_containers=RedisSchedulerContainerRepository(redis),
        scheduler_pool_states=RedisWorkerPoolStateRepository(redis),
    )
    bootstrap = _create_join_token(gateway, MachinePool("workspace-machine-pool"), workspace.id)
    joined = gateway.join_agent(_join_request(bootstrap.token))
    join_token_hash = hash_compute_token(bootstrap.token)
    agent_token_hash = hash_compute_token(joined.agent_token)
    orphan_revision = gateway.compute_states.keys.agent_route_revision(
        workspace.id,
        "implicit-worker-pool",
        "implicit-worker-machine",
    )
    gateway.compute_states.redis.set(orphan_revision, "1")
    scheduler_containers = RedisSchedulerContainerRepository(redis)
    ephemeral_container = SchedulerContainerState(
        container_id="build-workspace-delete-proof",
        workspace_id=workspace.id,
        stub_id="image-build",
    )
    scheduler_containers.set_container_state(ephemeral_container)
    scheduler_containers.update_container_status(
        ephemeral_container.container_id,
        SchedulerContainerStatus.Stopping,
    )
    assert scheduler_containers.delete_container_state(ephemeral_container.container_id)
    assert scheduler_containers.is_container_cancelled(ephemeral_container.container_id)
    ownership_index = scheduler_containers.keys.container_workspace_ownership_index(workspace.id)
    ownership_index_exists = redis.exists(ownership_index)

    with pytest.raises(ConflictError, match="lazycloud-agent leave"):
        WorkspaceDeletionService(services, gateway).delete(
            workspace.id,
            audit_actor=audit_actor,
        )

    assert control.get_workspace(workspace.id).status is WorkspaceStatus.Active
    assert gateway.compute_states.get_unit_state(workspace.id, unit.capacity_owner_id) is not None
    assert gateway.compute_states.get_join_token_state(join_token_hash) is not None
    assert gateway.compute_states.get_agent_token_state(agent_token_hash) is not None
    assert gateway.compute_states.redis.get(orphan_revision) == "1"
    assert scheduler_containers.is_container_cancelled(ephemeral_container.container_id)
    assert redis.exists(ownership_index) == ownership_index_exists
    with services.context.database.session() as session:
        assert MachineRepository(session).get_across_workspaces(joined.machine_id) is not None
        assert (
            ComputeMachineEnrollmentRepository(session).list_for_unit(
                workspace.id,
                unit.capacity_owner_id,
            )
            != []
        )
        assert (
            ComputeJoinCredentialRepository(session).list_for_unit(
                workspace.id,
                unit.capacity_owner_id,
            )
            != []
        )


def test_telemetry_usage_failure_does_not_advance_enrollment_cursor(
    isolated_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = _workspace_id(isolated_services)
    isolated_services.compute.create_unit(
        UnitName("metered-machines"),
        provider="agent",
        workspace=workspace_id,
    )
    gateway = _gateway(isolated_services)
    bootstrap = _create_join_token(gateway, MachinePool("metered-machines"), workspace_id)
    joined = gateway.join_agent(_join_request(bootstrap.token))
    state = gateway.compute_states.get_agent_token_state(hash_compute_token(joined.agent_token))
    assert state is not None
    gateway.compute_states.save_agent_token_state(
        state.model_copy(update={"last_join_at": utc_now() - timedelta(seconds=10)})
    )

    original_record = UsageService.record

    def fail_record(self: UsageService, **values: object) -> object:
        del self, values
        raise RuntimeError("usage store unavailable")

    monkeypatch.setattr(UsageService, "record", fail_record)
    with pytest.raises(RuntimeError, match="usage store unavailable"):
        gateway.stream_agent_telemetry(
            AgentTelemetryRequest(
                agent_token=joined.agent_token,
                metrics=AgentMetricSnapshot(memory_total_mb=16_384),
            )
        )

    with isolated_services.context.database.session() as session:
        failed = ComputeMachineEnrollmentRepository(session).by_machine(
            workspace_id,
            joined.machine_id,
            pool=MachinePool("metered-machines"),
        )
    assert failed is not None
    assert not failed.heartbeat_confirmed
    assert failed.last_heartbeat_at is None

    monkeypatch.setattr(UsageService, "record", original_record)
    response = gateway.stream_agent_telemetry(
        AgentTelemetryRequest(
            agent_token=joined.agent_token,
            metrics=AgentMetricSnapshot(memory_total_mb=16_384),
        )
    )
    assert response.ok
    with isolated_services.context.database.session() as session:
        saved = ComputeMachineEnrollmentRepository(session).by_machine(
            workspace_id,
            joined.machine_id,
            pool=MachinePool("metered-machines"),
        )
    assert saved is not None
    assert saved.heartbeat_confirmed
    assert saved.last_heartbeat_at is not None


def test_issuing_a_new_join_command_revokes_the_previous_credential(
    isolated_services: ApiServices,
) -> None:
    workspace_id = _workspace_id(isolated_services)
    isolated_services.compute.create_unit(
        UnitName("rotated-bootstrap"),
        provider="agent",
        workspace=workspace_id,
    )
    gateway = _gateway(isolated_services)
    previous = _create_join_token(gateway, MachinePool("rotated-bootstrap"), workspace_id)
    current = _create_join_token(gateway, MachinePool("rotated-bootstrap"), workspace_id)

    with pytest.raises(InvalidInputError, match="invalid or expired"):
        gateway.join_agent(_join_request(previous.token))
    assert gateway.join_agent(_join_request(current.token)).machine_id


def test_machine_join_command_owns_the_account_self_hosted_fleet(
    isolated_services: ApiServices,
) -> None:
    """A joined host belongs to the account, and one account has one fleet.

    The credential is minted for a person, not for wherever they happened to be, so
    a second workspace the same account owns resolves the same fleet and the same
    machine rather than a second copy of both.
    """
    workspace_id = _workspace_id(isolated_services)
    user_id = workspace_owner_user_id(isolated_services.context, workspace_id)
    isolated_services.control_plane_service.set_workspace(
        "second-workspace",
        owner_user_id=user_id,
    )
    gateway = _gateway(isolated_services)

    first = gateway.machine_join_command(
        MachineJoinCommandRequest(),
        user_id=user_id,
        owner_token_id="token-one",
    )

    assert gateway.gateway_endpoint.http_url in first.command
    fleets = [
        unit
        for unit in isolated_services.compute.list_units(workspace=workspace_id)
        if unit.pool == SELF_HOSTED_FLEET_POOL_NAME
    ]
    assert len(fleets) == 1
    assert fleets[0].provider == "agent"

    command_words = shlex.split(first.command)
    join_token = command_words[command_words.index("--join-token") + 1]
    joined = gateway.join_agent(_join_request(join_token))
    assert [machine.id for machine in gateway.account_machine_views(user_id)] == [joined.machine_id]
    with isolated_services.context.database.session() as session:
        enrollment = ComputeMachineEnrollmentRepository(session).by_machine(
            workspace_id,
            joined.machine_id,
        )
    assert enrollment is not None
    assert enrollment.user_id == user_id

    second = gateway.machine_join_command(
        MachineJoinCommandRequest(gpu=["A10G"]),
        user_id=user_id,
        owner_token_id="token-two",
    )

    assert second.command
    fleets = [
        unit
        for unit in isolated_services.compute.list_units(workspace=workspace_id)
        if unit.pool == SELF_HOSTED_FLEET_POOL_NAME
    ]
    assert len(fleets) == 1
    assert fleets[0].worker_gpu_type == "A10G"

    with isolated_services.context.database.session() as session:
        credentials = ComputeJoinCredentialRepository(session).list_for_unit(
            workspace_id,
            fleets[0].capacity_owner_id,
        )
    used, active = sorted(credentials, key=lambda credential: credential.created_at)
    assert used.status is ComputeCredentialStatus.Revoked
    assert active.status is ComputeCredentialStatus.Active
    assert {credential.user_id for credential in credentials} == {user_id}


@pytest.mark.anyio
async def test_a_machine_that_stops_reporting_is_written_off_once_and_told_to_its_owner(
    async_services: ApiServices,
) -> None:
    async_io = async_services.require_async_io()
    # Every other enrollment write happens because a heartbeat arrived, which is
    # the one thing a machine that has gone does not do. Without the sweep the row
    # keeps saying Ready for a host that is switched off, and the only place the
    # truth appears is a view that recomputes it per request and writes nothing.
    workspace_id = _workspace_id(async_services)
    pool = MachinePool("silent-machines")
    async_services.compute.create_unit(UnitName(pool), provider="agent", workspace=workspace_id)
    gateway = _gateway(async_services)
    bootstrap = _create_join_token(gateway, pool, workspace_id)
    joined = gateway.join_agent(_join_request(bootstrap.token))
    _bind_private_network(gateway, workspace_id, joined.agent_token, joined.machine_id)
    assert gateway.stream_agent(StreamAgentRequest(agent_token=joined.agent_token)).ok
    # A platform defect belongs to the platform, so it carries no workspace. It is
    # here to prove the customer's feed does not fold those in.
    async_services.events.emit(
        "billing.span.unpriced",
        resource_type="usage",
        resource_id="span",
        message="cluster event",
    )
    silent_at = utc_now() + timedelta(minutes=5)

    marked = await gateway.sweep_disconnected_agents(
        async_io.database,
        async_io.redis,
        now=silent_at,
    )
    repeated = await gateway.sweep_disconnected_agents(
        async_io.database,
        async_io.redis,
        now=silent_at,
    )

    assert marked == [joined.machine_id]
    assert repeated == []
    with async_services.context.database.session() as session:
        enrollment = ComputeMachineEnrollmentRepository(session).by_machine(
            workspace_id,
            joined.machine_id,
            pool=pool,
        )
        machine = MachineRepository(session).get(joined.machine_id, workspace_id=workspace_id)
    assert enrollment is not None
    assert enrollment.readiness_phase is MachineReadinessPhase.Offline
    assert enrollment.last_disconnect_at is not None
    assert machine is not None
    assert machine.status is ResourceStatus.Stopped

    # Read the way every customer event route reads, rather than through the
    # repository default: the leak this closes was a keyword the routes passed.
    visible = ManagementService(async_services).event_history(workspace_id)
    actions = [event.action for event in visible.data]
    assert actions.count("agent.disconnected") == 1
    assert "billing.span.unpriced" not in actions

    # The heartbeat clears the disconnect, so a host that comes back is Ready again
    # rather than staying written off until someone notices.
    assert gateway.stream_agent(StreamAgentRequest(agent_token=joined.agent_token)).ok
    recovered = _pool_machines(gateway, pool, workspace_id)[0]
    assert recovered.readiness_phase is MachineReadinessPhase.Ready
