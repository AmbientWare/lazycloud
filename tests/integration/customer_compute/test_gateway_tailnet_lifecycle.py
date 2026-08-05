from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from datetime import timedelta
from threading import Event

import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from api.server.workspace_deletion import WorkspaceDeletionService
from compute.agent_control import TailnetConfig, hash_compute_token
from compute.state import RedisComputeStateRepository
from control.service import ControlPlaneService
from coordination.redis_client import RedisClient
from database.repositories.compute import ComputeMachineEnrollmentRepository
from database.repositories.orchestration import MachineRepository
from database.tailnet_cleanup import DatabaseTailnetCleanupStore
from fastapi.testclient import TestClient
from gateway.http import (
    JoinAgentRequest,
    LeaveAgentRequest,
    RegisterAgentTailnetDeviceRequest,
    RequestAgentTransportCredentialRequest,
    StreamAgentRequest,
)
from gateway.service import GatewayControlService
from identity.auth import AuthService
from networking.tailnet_cleanup import TailnetCleanupCoordinator
from networking.tailnet_control import (
    TailnetAuthKey,
    TailnetControlError,
    TailnetControlErrorCode,
    TailnetDevice,
)
from pydantic import SecretStr
from scheduler.capacity_reservations import (
    CapacityReservationService,
    RedisCapacityReservationRepository,
)
from scheduler.state import (
    RedisSchedulerContainerRepository,
    RedisSchedulerWorkerRepository,
    RedisWorkerPoolStateRepository,
)
from shared.compute_enrollment import (
    ComputePreflightCheck,
    PreflightSeverity,
    TailnetEnrollmentPhase,
)
from shared.compute_policy import MachinePool, UnitName
from shared.errors import ConflictError, InvalidInputError, UpstreamUnavailableError
from shared.identity import TokenKind, WorkspaceStatus
from shared.routing import BackendRouteTransport
from shared.timestamps import utc_now
from tests.real_redis import RealRedisActors
from tests.redis_fakes import FakeRedis


@pytest.fixture
def client_stack() -> Iterator[ExitStack]:
    with ExitStack() as stack:
        yield stack


@dataclass(slots=True)
class _RecordingTailnetControl:
    devices: dict[str, TailnetDevice] = field(default_factory=dict)
    issued: list[tuple[str, str, str]] = field(default_factory=list)
    revoked_key_ids: list[str] = field(default_factory=list)
    verified: list[tuple[str, str]] = field(default_factory=list)
    removed_device_ids: list[str] = field(default_factory=list)
    find_hostnames: list[str] = field(default_factory=list)
    remove_error: TailnetControlError | None = None
    find_error: TailnetControlError | None = None
    blocked_issue_call: int = 0
    issue_started: Event = field(default_factory=Event)
    issue_release: Event = field(default_factory=Event)
    block_verification: bool = False
    verification_started: Event = field(default_factory=Event)
    verification_release: Event = field(default_factory=Event)

    def issue_auth_key(self, *, machine_id: str, hostname: str) -> TailnetAuthKey:
        key_id = f"key-{machine_id}-{len(self.issued) + 1}"
        self.issued.append((key_id, machine_id, hostname))
        if len(self.issued) == self.blocked_issue_call:
            self.issue_started.set()
            if not self.issue_release.wait(timeout=5):
                raise RuntimeError("timed out waiting to release tailnet auth-key issuance")
        return TailnetAuthKey(
            id=key_id,
            key=SecretStr(f"one-off-{key_id}"),
            expires_at=utc_now() + timedelta(minutes=5),
        )

    def revoke_auth_key(self, key_id: str) -> None:
        if key_id not in self.revoked_key_ids:
            self.revoked_key_ids.append(key_id)

    def verify_device(self, node_id: str, *, expected_hostname: str) -> TailnetDevice:
        self.verified.append((node_id, expected_hostname))
        device = self.devices.get(node_id)
        if device is None:
            raise TailnetControlError(
                TailnetControlErrorCode.VerificationFailed,
                "device does not belong to this machine",
                retryable=False,
            )
        if self.block_verification:
            self.verification_started.set()
            if not self.verification_release.wait(timeout=5):
                raise RuntimeError("timed out waiting to release tailnet device verification")
        return device

    def find_devices(self, *, hostname: str) -> tuple[TailnetDevice, ...]:
        self.find_hostnames.append(hostname)
        if self.find_error is not None:
            raise self.find_error
        return tuple(device for device in self.devices.values() if device.hostname == hostname)

    def remove_device(self, device_id: str) -> None:
        if self.remove_error is not None:
            raise self.remove_error
        if device_id not in self.removed_device_ids:
            self.removed_device_ids.append(device_id)
        for node_id, device in tuple(self.devices.items()):
            if device.id == device_id:
                del self.devices[node_id]


@dataclass(frozen=True, slots=True)
class _EnrolledMachine:
    workspace_id: str
    pool: MachinePool
    unit_id: str
    machine_id: str
    agent_token: str


def _gateway(
    services: ApiServices,
    control: _RecordingTailnetControl,
    *,
    key_prefix: str,
    redis: RedisClient | None = None,
) -> GatewayControlService:
    selected_redis = redis or RedisClient(FakeRedis(), key_prefix=key_prefix)
    return replace(
        services.gateway_service,
        compute_state=RedisComputeStateRepository(selected_redis),
        scheduler_workers=RedisSchedulerWorkerRepository(selected_redis),
        scheduler_containers=RedisSchedulerContainerRepository(selected_redis),
        scheduler_pool_states=RedisWorkerPoolStateRepository(selected_redis),
        capacity_reservations=CapacityReservationService(
            RedisCapacityReservationRepository(selected_redis),
            lambda: [],
        ),
        tailnet=TailnetConfig(),
        tailnet_control=control,
    )


def _operator_auth(services: ApiServices, *, workspace_id: str = "default") -> dict[str, str]:
    token, _ = AuthService(services.context).create_token(
        "tailnet-operator-delete",
        kind=TokenKind.Admin,
        workspace_id=workspace_id,
    )
    return {"Authorization": f"Bearer {token}"}


def _enroll(
    services: ApiServices,
    gateway: GatewayControlService,
    *,
    pool: str,
    fingerprint: str = "customer-machine",
    workspace_id: str | None = None,
) -> _EnrolledMachine:
    if workspace_id is None:
        with services.context.database.session() as session:
            workspace_id = services.context.default_workspace_id(session)
    unit = services.compute.create_unit(
        UnitName(pool), provider="agent", workspace=workspace_id
    )
    bootstrap = gateway.unit_state_coordinator.create_unit_join_token(
        UnitName(pool),
        workspace_id=workspace_id,
        owner_token_id="tailnet-lifecycle-test",
    )
    joined = gateway.join_agent(
        JoinAgentRequest(
            join_token=bootstrap.token,
            machine_fingerprint=fingerprint,
            hostname="untrusted-host",
            os="linux",
            arch="amd64",
            cpu_count=4,
            memory_mb=8_192,
            preflight=[
                ComputePreflightCheck(
                    name="container-runtime",
                    ok=True,
                    severity=PreflightSeverity.Error,
                )
            ],
        )
    )
    return _EnrolledMachine(
        workspace_id=workspace_id,
        pool=unit.pool,
        unit_id=unit.id,
        machine_id=joined.machine_id,
        agent_token=joined.agent_token,
    )


def _leave(gateway: GatewayControlService, enrolled: _EnrolledMachine) -> None:
    response = gateway.leave_agent(
        LeaveAgentRequest(
            agent_token=enrolled.agent_token,
            machine_id=enrolled.machine_id,
        )
    )
    assert response.machine_id == enrolled.machine_id


def test_resource_pool_delete_runs_canonical_cleanup_and_rescans_late_device(
    isolated_services: ApiServices,
    client_stack: ExitStack,
    real_redis_actors: RealRedisActors,
) -> None:
    control = _RecordingTailnetControl()
    gateway = _gateway(
        isolated_services,
        control,
        key_prefix="resource-pool-delete",
        redis=real_redis_actors.client(),
    )
    enrolled = _enroll(isolated_services, gateway, pool="resource-pool-delete")
    _, hostname = _issue(gateway, enrolled)
    _leave(gateway, enrolled)
    client = client_stack.enter_context(
        TestClient(create_app(isolated_services, gateway_service=gateway))
    )

    response = client.delete(
        f"/api/v1/units/{enrolled.unit_id}",
        headers=_operator_auth(isolated_services),
    )

    assert response.status_code == 204
    store = DatabaseTailnetCleanupStore(isolated_services.context)
    tombstone = store.get_by_machine(enrolled.machine_id)
    assert tombstone is not None
    assert isolated_services.compute.list_units(workspace=enrolled.workspace_id) == []
    control.devices["late-node"] = TailnetDevice(
        id="late-device",
        node_id="late-node",
        hostname=hostname,
        authorized=True,
    )
    batch = TailnetCleanupCoordinator(store, control).reconcile_due(now=tombstone.not_before)
    assert batch.completed_count == 1
    assert control.removed_device_ids == ["late-device"]


def test_resource_pool_delete_requires_host_leave_before_mutation(
    isolated_services: ApiServices,
    client_stack: ExitStack,
    real_redis_actors: RealRedisActors,
) -> None:
    control = _RecordingTailnetControl()
    gateway = _gateway(
        isolated_services,
        control,
        key_prefix="resource-pool-delete-retry",
        redis=real_redis_actors.client(),
    )
    enrolled = _enroll(isolated_services, gateway, pool="resource-pool-delete-retry")
    _issue(gateway, enrolled)
    blocked_gateway = replace(gateway, tailnet_control=None)
    client = client_stack.enter_context(
        TestClient(create_app(isolated_services, gateway_service=blocked_gateway))
    )

    blocked = client.delete(
        f"/api/v1/units/{enrolled.unit_id}",
        headers=_operator_auth(isolated_services),
    )

    assert blocked.status_code == 409
    assert [unit.name for unit in isolated_services.compute.list_units()] == [enrolled.pool]
    with isolated_services.context.database.session() as session:
        assert (
            ComputeMachineEnrollmentRepository(session).by_machine(
                enrolled.workspace_id,
                enrolled.machine_id,
                pool=enrolled.pool,
            )
            is not None
        )

    retry_gateway = replace(gateway, tailnet_control=control)
    _leave(retry_gateway, enrolled)
    retry_client = client_stack.enter_context(
        TestClient(create_app(isolated_services, gateway_service=retry_gateway))
    )
    retried = retry_client.delete(
        f"/api/v1/units/{enrolled.unit_id}",
        headers=_operator_auth(isolated_services),
    )
    assert retried.status_code == 204


def test_resource_pool_delete_cannot_delete_another_workspace_pool(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    control_plane = ControlPlaneService(isolated_services.context)
    control_plane.upsert_workspace("default")
    foreign = control_plane.upsert_workspace("foreign-pool-owner")
    isolated_services.compute.create_unit(
        UnitName("foreign-pool"),
        provider="agent",
        workspace=foreign.id,
    )
    gateway = _gateway(
        isolated_services,
        _RecordingTailnetControl(),
        key_prefix="resource-pool-delete-scope",
    )
    client = client_stack.enter_context(
        TestClient(create_app(isolated_services, gateway_service=gateway))
    )

    response = client.delete(
        "/api/v1/units/foreign-unit",
        headers=_operator_auth(isolated_services),
    )

    assert response.status_code == 404
    assert [unit.name for unit in isolated_services.compute.list_units(workspace=foreign.id)] == [
        "foreign-pool"
    ]


def test_workspace_deletion_conflicts_before_self_hosted_tailnet_authority_changes(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    control_plane = ControlPlaneService(isolated_services.context)
    actor = control_plane.upsert_workspace("default")
    _raw_token, audit_actor = AuthService(isolated_services.context).create_token(
        "workspace-delete-admin",
        kind=TokenKind.Admin,
        workspace_id=actor.id,
    )
    workspace = control_plane.upsert_workspace("tailnet-workspace-delete")
    control = _RecordingTailnetControl()
    gateway = _gateway(
        isolated_services,
        control,
        key_prefix="tailnet-workspace-delete",
        redis=real_redis_actors.client(),
    )
    enrolled = _enroll(
        isolated_services,
        gateway,
        pool="tailnet-workspace-delete",
        workspace_id=workspace.id,
    )
    _issue(gateway, enrolled)
    key_id = control.issued[-1][0]

    with pytest.raises(ConflictError, match="lazycloud-agent leave"):
        WorkspaceDeletionService(isolated_services, gateway).delete(
            workspace.id,
            audit_actor=audit_actor,
        )

    store = DatabaseTailnetCleanupStore(isolated_services.context)
    tombstone = store.get_by_machine(enrolled.machine_id)
    assert tombstone is None
    assert key_id not in control.revoked_key_ids
    assert control_plane.get_workspace(workspace.id).status is WorkspaceStatus.Active
    with isolated_services.context.database.session() as session:
        assert (
            ComputeMachineEnrollmentRepository(session).by_machine(
                enrolled.workspace_id,
                enrolled.machine_id,
                pool=enrolled.pool,
            )
            is not None
        )


def _issue(
    gateway: GatewayControlService,
    enrolled: _EnrolledMachine,
) -> tuple[str, str]:
    credential = gateway.request_agent_transport_credential(
        RequestAgentTransportCredentialRequest(
            agent_token=enrolled.agent_token,
            transport=BackendRouteTransport.TsnetRestricted,
        )
    )
    return credential.auth_key, credential.hostname


def _bind(
    gateway: GatewayControlService,
    control: _RecordingTailnetControl,
    enrolled: _EnrolledMachine,
    *,
    node_id: str,
    device_id: str,
    hostname: str,
    address: str,
) -> None:
    control.devices[node_id] = TailnetDevice(
        id=device_id,
        node_id=node_id,
        hostname=hostname,
        name=f"{hostname}.example.ts.net",
        addresses=(address,),
        tags=("tag:lazycloud-agent",),
        authorized=True,
    )
    gateway.register_agent_tailnet_device(
        RegisterAgentTailnetDeviceRequest(
            agent_token=enrolled.agent_token,
            node_id=node_id,
        )
    )


def test_transport_credential_is_one_off_and_supersedes_unused_key(
    isolated_services: ApiServices,
) -> None:
    control = _RecordingTailnetControl()
    gateway = _gateway(isolated_services, control, key_prefix="tailnet-key-lifecycle")
    enrolled = _enroll(
        isolated_services,
        gateway,
        pool="tailnet-key-lifecycle",
    )

    first_secret, expected_hostname = _issue(gateway, enrolled)
    second_secret, second_hostname = _issue(gateway, enrolled)

    first_key_id, issued_machine_id, issued_hostname = control.issued[0]
    second_key_id, _, _ = control.issued[1]
    assert first_secret == f"one-off-{first_key_id}"
    assert second_secret == f"one-off-{second_key_id}"
    assert first_secret != second_secret
    assert issued_machine_id == enrolled.machine_id
    assert issued_hostname == expected_hostname
    assert second_hostname != expected_hostname
    assert control.issued[1][2] == second_hostname
    assert control.revoked_key_ids == [first_key_id]

    with isolated_services.context.database.session() as session:
        persisted = ComputeMachineEnrollmentRepository(session).by_machine(
            enrolled.workspace_id,
            enrolled.machine_id,
            pool=enrolled.pool,
        )
    assert persisted is not None
    assert persisted.tailnet_auth_key_id == second_key_id
    assert persisted.tailnet_auth_key_expires_at is not None
    assert persisted.tailnet_hostname == second_hostname
    assert persisted.tailnet_device_id == ""
    assert persisted.tailnet_generation == 2
    assert persisted.tailnet_phase is TailnetEnrollmentPhase.AwaitingDevice
    assert persisted.tailnet_cleanup_auth_key_ids == []
    assert persisted.tailnet_cleanup_device_ids == []
    assert first_secret not in persisted.model_dump_json()
    assert second_secret not in persisted.model_dump_json()


def test_verified_provider_device_is_required_and_agent_claims_are_ignored(
    isolated_services: ApiServices,
) -> None:
    control = _RecordingTailnetControl()
    gateway = _gateway(isolated_services, control, key_prefix="tailnet-device-binding")
    enrolled = _enroll(
        isolated_services,
        gateway,
        pool="tailnet-device-binding",
    )

    before_binding = gateway.stream_agent(StreamAgentRequest(agent_token=enrolled.agent_token))
    assert not before_binding.ok
    assert before_binding.err_msg == "agent tailnet identity is not verified"

    _, expected_hostname = _issue(gateway, enrolled)
    with pytest.raises(InvalidInputError, match="could not be verified"):
        gateway.register_agent_tailnet_device(
            RegisterAgentTailnetDeviceRequest(
                agent_token=enrolled.agent_token,
                node_id="foreign-node",
            )
        )

    control.devices["mismatched-node"] = TailnetDevice(
        id="mismatched-device",
        node_id="different-stable-node",
        hostname=expected_hostname,
        addresses=("100.64.0.11",),
        tags=("tag:lazycloud-agent",),
        authorized=True,
    )
    with pytest.raises(InvalidInputError, match="could not be verified"):
        gateway.register_agent_tailnet_device(
            RegisterAgentTailnetDeviceRequest(
                agent_token=enrolled.agent_token,
                node_id="mismatched-node",
            )
        )

    _bind(
        gateway,
        control,
        enrolled,
        node_id="verified-node",
        device_id="verified-device",
        hostname=expected_hostname,
        address="100.64.0.12",
    )

    with isolated_services.context.database.session() as session:
        persisted = ComputeMachineEnrollmentRepository(session).by_machine(
            enrolled.workspace_id,
            enrolled.machine_id,
            pool=enrolled.pool,
        )
    assert persisted is not None
    assert persisted.tailnet_device_id == "verified-device"
    assert persisted.tailnet_hostname == expected_hostname
    assert persisted.tailnet_ips == ["100.64.0.12"]
    assert persisted.tailnet_verified_at is not None
    assert persisted.tailnet_phase is TailnetEnrollmentPhase.Bound
    assert gateway.stream_agent(StreamAgentRequest(agent_token=enrolled.agent_token)).ok


def test_restart_reuses_verified_device_without_issuing_another_key(
    isolated_services: ApiServices,
) -> None:
    control = _RecordingTailnetControl()
    gateway = _gateway(isolated_services, control, key_prefix="tailnet-restart")
    enrolled = _enroll(isolated_services, gateway, pool="tailnet-restart")
    _, expected_hostname = _issue(gateway, enrolled)
    _bind(
        gateway,
        control,
        enrolled,
        node_id="durable-node",
        device_id="durable-device",
        hostname=expected_hostname,
        address="100.64.0.20",
    )

    assert gateway.stream_agent(StreamAgentRequest(agent_token=enrolled.agent_token)).ok
    gateway.compute_states.delete_agent_token_state(hash_compute_token(enrolled.agent_token))
    assert gateway.stream_agent(StreamAgentRequest(agent_token=enrolled.agent_token)).ok
    assert len(control.issued) == 1
    assert control.removed_device_ids == []


def test_stale_registration_cannot_restore_device_removed_by_rotation(
    isolated_services: ApiServices,
) -> None:
    control = _RecordingTailnetControl()
    gateway = _gateway(isolated_services, control, key_prefix="tailnet-register-rotation-race")
    enrolled = _enroll(isolated_services, gateway, pool="register-rotation-race")
    _, first_hostname = _issue(gateway, enrolled)
    _bind(
        gateway,
        control,
        enrolled,
        node_id="first-node",
        device_id="first-device",
        hostname=first_hostname,
        address="100.64.0.21",
    )
    control.block_verification = True

    with ThreadPoolExecutor(max_workers=1) as executor:
        stale_registration = executor.submit(
            gateway.register_agent_tailnet_device,
            RegisterAgentTailnetDeviceRequest(
                agent_token=enrolled.agent_token,
                node_id="first-node",
            ),
        )
        assert control.verification_started.wait(timeout=5)
        _issue(gateway, enrolled)
        control.devices["late-stale-node"] = TailnetDevice(
            id="late-stale-device",
            node_id="late-stale-node",
            hostname=first_hostname,
            authorized=True,
        )
        control.verification_release.set()
        with pytest.raises(InvalidInputError, match="no longer current"):
            stale_registration.result(timeout=5)

    with isolated_services.context.database.session() as session:
        persisted = ComputeMachineEnrollmentRepository(session).by_machine(
            enrolled.workspace_id,
            enrolled.machine_id,
            pool=enrolled.pool,
        )
    assert persisted is not None
    assert persisted.tailnet_generation == 2
    assert persisted.tailnet_phase is TailnetEnrollmentPhase.AwaitingDevice
    assert persisted.tailnet_device_id == ""
    assert control.removed_device_ids == ["first-device", "late-stale-device"]


def test_rotation_reconciles_device_created_before_registration_process_loss(
    isolated_services: ApiServices,
) -> None:
    control = _RecordingTailnetControl()
    gateway = _gateway(isolated_services, control, key_prefix="tailnet-unregistered-rotation")
    enrolled = _enroll(isolated_services, gateway, pool="unregistered-rotation")
    _, abandoned_hostname = _issue(gateway, enrolled)
    control.devices["abandoned-node"] = TailnetDevice(
        id="abandoned-device",
        node_id="abandoned-node",
        hostname=abandoned_hostname,
        authorized=True,
    )

    _, current_hostname = _issue(gateway, enrolled)

    assert current_hostname.endswith("-g2")
    assert control.find_hostnames == [abandoned_hostname]
    assert control.removed_device_ids == ["abandoned-device"]
    assert control.devices == {}


def test_new_rotation_supersedes_in_flight_generation(
    isolated_services: ApiServices,
) -> None:
    control = _RecordingTailnetControl(blocked_issue_call=1)
    gateway = _gateway(isolated_services, control, key_prefix="tailnet-concurrent-rotation")
    enrolled = _enroll(isolated_services, gateway, pool="concurrent-rotation")

    with ThreadPoolExecutor(max_workers=1) as executor:
        first_rotation = executor.submit(_issue, gateway, enrolled)
        assert control.issue_started.wait(timeout=5)
        second_secret, second_hostname = _issue(gateway, enrolled)
        control.issue_release.set()
        with pytest.raises(InvalidInputError, match="no longer current"):
            first_rotation.result(timeout=5)

    with isolated_services.context.database.session() as session:
        persisted = ComputeMachineEnrollmentRepository(session).by_machine(
            enrolled.workspace_id,
            enrolled.machine_id,
            pool=enrolled.pool,
        )
    assert persisted is not None
    assert persisted.tailnet_generation == 2
    assert persisted.tailnet_phase is TailnetEnrollmentPhase.AwaitingDevice
    assert persisted.tailnet_auth_key_id == control.issued[1][0]
    assert second_secret == f"one-off-{control.issued[1][0]}"
    assert second_hostname.endswith("-g2")
    assert control.revoked_key_ids == [control.issued[0][0]]
    assert persisted.tailnet_cleanup_auth_key_ids == []


def test_stale_rotating_generation_is_recoverable_after_process_loss(
    isolated_services: ApiServices,
) -> None:
    control = _RecordingTailnetControl()
    gateway = _gateway(isolated_services, control, key_prefix="tailnet-crash-recovery")
    enrolled = _enroll(isolated_services, gateway, pool="crash-recovery")
    stale_device = TailnetDevice(
        id="stale-device",
        node_id="stale-node",
        hostname="stale-hostname",
        authorized=True,
    )
    control.devices[stale_device.node_id] = stale_device
    with isolated_services.context.database.session() as session:
        enrollments = ComputeMachineEnrollmentRepository(session)
        persisted = enrollments.by_machine(
            enrolled.workspace_id,
            enrolled.machine_id,
            pool=enrolled.pool,
            for_update=True,
        )
        assert persisted is not None
        enrollments.save(
            persisted.model_copy(
                update={
                    "tailnet_generation": 7,
                    "tailnet_phase": TailnetEnrollmentPhase.Rotating,
                    "tailnet_hostname": "abandoned-generation",
                    "tailnet_cleanup_auth_key_ids": ["stale-key"],
                    "tailnet_cleanup_device_ids": [stale_device.id],
                }
            )
        )

    _issue(gateway, enrolled)

    with isolated_services.context.database.session() as session:
        recovered = ComputeMachineEnrollmentRepository(session).by_machine(
            enrolled.workspace_id,
            enrolled.machine_id,
            pool=enrolled.pool,
        )
    assert recovered is not None
    assert recovered.tailnet_generation == 8
    assert recovered.tailnet_phase is TailnetEnrollmentPhase.AwaitingDevice
    assert recovered.tailnet_cleanup_auth_key_ids == []
    assert recovered.tailnet_cleanup_device_ids == []
    assert control.revoked_key_ids == ["stale-key"]
    assert control.removed_device_ids == ["stale-device"]


def test_failed_rotation_retains_cleanup_ownership_for_retry(
    isolated_services: ApiServices,
) -> None:
    control = _RecordingTailnetControl()
    gateway = _gateway(isolated_services, control, key_prefix="tailnet-rotation-retry")
    enrolled = _enroll(isolated_services, gateway, pool="rotation-retry")
    _, hostname = _issue(gateway, enrolled)
    first_key_id = control.issued[0][0]
    _bind(
        gateway,
        control,
        enrolled,
        node_id="retry-node",
        device_id="retry-device",
        hostname=hostname,
        address="100.64.0.22",
    )
    control.remove_error = TailnetControlError(
        TailnetControlErrorCode.UpstreamUnavailable,
        "coordination service unavailable",
        retryable=True,
    )

    with pytest.raises(UpstreamUnavailableError, match="enrollment is unavailable"):
        _issue(gateway, enrolled)

    with isolated_services.context.database.session() as session:
        failed = ComputeMachineEnrollmentRepository(session).by_machine(
            enrolled.workspace_id,
            enrolled.machine_id,
            pool=enrolled.pool,
        )
    assert failed is not None
    assert failed.tailnet_phase is TailnetEnrollmentPhase.Failed
    assert failed.tailnet_device_id == ""
    assert failed.tailnet_auth_key_id == ""
    assert failed.tailnet_cleanup_device_ids == ["retry-device"]
    assert failed.tailnet_cleanup_auth_key_ids == [first_key_id]

    control.remove_error = None
    _issue(gateway, enrolled)

    with isolated_services.context.database.session() as session:
        recovered = ComputeMachineEnrollmentRepository(session).by_machine(
            enrolled.workspace_id,
            enrolled.machine_id,
            pool=enrolled.pool,
        )
    assert recovered is not None
    assert recovered.tailnet_phase is TailnetEnrollmentPhase.AwaitingDevice
    assert recovered.tailnet_cleanup_device_ids == []
    assert recovered.tailnet_cleanup_auth_key_ids == []
    assert control.removed_device_ids == ["retry-device"]
    assert control.revoked_key_ids == [first_key_id]


def test_terminal_deletion_supersedes_in_flight_rotation(
    isolated_services: ApiServices,
) -> None:
    control = _RecordingTailnetControl(blocked_issue_call=1)
    gateway = _gateway(isolated_services, control, key_prefix="tailnet-delete-rotation-race")
    enrolled = _enroll(isolated_services, gateway, pool="delete-rotation-race")

    with ThreadPoolExecutor(max_workers=1) as executor:
        rotation = executor.submit(_issue, gateway, enrolled)
        assert control.issue_started.wait(timeout=5)
        _leave(gateway, enrolled)
        control.issue_release.set()
        with pytest.raises(InvalidInputError, match="no longer current"):
            rotation.result(timeout=5)

    with isolated_services.context.database.session() as session:
        persisted = ComputeMachineEnrollmentRepository(session).by_machine(
            enrolled.workspace_id,
            enrolled.machine_id,
            pool=enrolled.pool,
        )
    assert persisted is None
    assert control.revoked_key_ids == [control.issued[0][0]]


def test_terminal_deletion_cleans_abandoned_rotation_resources(
    isolated_services: ApiServices,
) -> None:
    control = _RecordingTailnetControl()
    gateway = _gateway(isolated_services, control, key_prefix="tailnet-delete-abandoned")
    enrolled = _enroll(isolated_services, gateway, pool="delete-abandoned")
    abandoned_device = TailnetDevice(
        id="abandoned-device",
        node_id="abandoned-node",
        hostname="abandoned-generation",
        authorized=True,
    )
    control.devices[abandoned_device.node_id] = abandoned_device
    with isolated_services.context.database.session() as session:
        enrollments = ComputeMachineEnrollmentRepository(session)
        persisted = enrollments.by_machine(
            enrolled.workspace_id,
            enrolled.machine_id,
            pool=enrolled.pool,
            for_update=True,
        )
        assert persisted is not None
        enrollments.save(
            persisted.model_copy(
                update={
                    "tailnet_generation": 7,
                    "tailnet_phase": TailnetEnrollmentPhase.Rotating,
                    "tailnet_hostname": abandoned_device.hostname,
                    "tailnet_cleanup_auth_key_ids": ["abandoned-key"],
                    "tailnet_cleanup_device_ids": [abandoned_device.id],
                }
            )
        )

    _leave(gateway, enrolled)

    with isolated_services.context.database.session() as session:
        persisted = ComputeMachineEnrollmentRepository(session).by_machine(
            enrolled.workspace_id,
            enrolled.machine_id,
            pool=enrolled.pool,
        )
    assert persisted is None
    assert control.removed_device_ids == [abandoned_device.id]
    assert control.revoked_key_ids == ["abandoned-key"]


def test_terminal_cleanup_discovers_unregistered_device_and_retries(
    isolated_services: ApiServices,
) -> None:
    control = _RecordingTailnetControl()
    gateway = _gateway(isolated_services, control, key_prefix="tailnet-unregistered-delete")
    enrolled = _enroll(isolated_services, gateway, pool="unregistered-delete")
    _, hostname = _issue(gateway, enrolled)
    control.devices["unregistered-node"] = TailnetDevice(
        id="unregistered-device",
        node_id="unregistered-node",
        hostname=hostname,
        authorized=True,
    )
    control.find_error = TailnetControlError(
        TailnetControlErrorCode.UpstreamUnavailable,
        "coordination service unavailable",
        retryable=True,
    )

    _leave(gateway, enrolled)

    store = DatabaseTailnetCleanupStore(isolated_services.context)
    retained = store.get_by_machine(enrolled.machine_id)
    assert retained is not None
    assert control.removed_device_ids == []

    control.find_error = None
    batch = TailnetCleanupCoordinator(store, control).reconcile_due(
        now=retained.not_before,
    )

    assert batch.completed_count == 1
    assert control.removed_device_ids == ["unregistered-device"]
    assert control.find_hostnames == [hostname, hostname]
    assert store.get_by_machine(enrolled.machine_id) is None
    with isolated_services.context.database.session() as session:
        assert MachineRepository(session).get_across_workspaces(enrolled.machine_id) is None


def test_terminal_cleanup_retains_tombstone_for_device_visible_after_initial_scan(
    isolated_services: ApiServices,
) -> None:
    control = _RecordingTailnetControl()
    gateway = _gateway(isolated_services, control, key_prefix="tailnet-late-visibility")
    enrolled = _enroll(isolated_services, gateway, pool="late-visibility")
    _, hostname = _issue(gateway, enrolled)

    _leave(gateway, enrolled)

    store = DatabaseTailnetCleanupStore(isolated_services.context)
    tombstone = store.get_by_machine(enrolled.machine_id)
    assert tombstone is not None
    assert control.find_hostnames == [hostname]
    control.devices["eventually-visible-node"] = TailnetDevice(
        id="eventually-visible-device",
        node_id="eventually-visible-node",
        hostname=hostname,
        authorized=True,
    )

    before_expiry = TailnetCleanupCoordinator(store, control).reconcile_due(
        now=tombstone.not_before - timedelta(seconds=1),
    )
    after_expiry = TailnetCleanupCoordinator(store, control).reconcile_due(
        now=tombstone.not_before,
    )

    assert before_expiry.processed_count == 0
    assert after_expiry.completed_count == 1
    assert control.removed_device_ids == ["eventually-visible-device"]
    assert store.get_by_machine(enrolled.machine_id) is None


def test_terminal_hostname_reconciliation_is_machine_scoped(
    isolated_services: ApiServices,
) -> None:
    control = _RecordingTailnetControl()
    gateway = _gateway(isolated_services, control, key_prefix="tailnet-machine-isolation")
    first = _enroll(
        isolated_services,
        gateway,
        pool="machine-isolation-first",
        fingerprint="machine-isolation-first",
    )
    second = _enroll(
        isolated_services,
        gateway,
        pool="machine-isolation-second",
        fingerprint="machine-isolation-second",
    )
    _, first_hostname = _issue(gateway, first)
    _, second_hostname = _issue(gateway, second)
    control.devices["first-node"] = TailnetDevice(
        id="first-device",
        node_id="first-node",
        hostname=first_hostname,
        authorized=True,
    )
    control.devices["second-node"] = TailnetDevice(
        id="second-device",
        node_id="second-node",
        hostname=second_hostname,
        authorized=True,
    )

    _leave(gateway, first)

    assert control.find_hostnames == [first_hostname]
    assert control.removed_device_ids == ["first-device"]
    assert tuple(device.id for device in control.devices.values()) == ("second-device",)


def test_machine_and_pool_deletion_remove_tailnet_identity_and_key(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    control = _RecordingTailnetControl()
    gateway = _gateway(
        isolated_services,
        control,
        key_prefix="tailnet-cleanup",
        redis=real_redis_actors.client(),
    )
    machine = _enroll(isolated_services, gateway, pool="machine-cleanup")
    _, machine_hostname = _issue(gateway, machine)
    machine_key_id = control.issued[-1][0]
    _bind(
        gateway,
        control,
        machine,
        node_id="machine-node",
        device_id="machine-device",
        hostname=machine_hostname,
        address="100.64.0.30",
    )

    _leave(gateway, machine)
    assert control.removed_device_ids == ["machine-device"]
    assert control.revoked_key_ids == [machine_key_id]

    pool_machine = _enroll(isolated_services, gateway, pool="pool-cleanup")
    _, pool_hostname = _issue(gateway, pool_machine)
    pool_key_id = control.issued[-1][0]
    _bind(
        gateway,
        control,
        pool_machine,
        node_id="pool-node",
        device_id="pool-device",
        hostname=pool_hostname,
        address="100.64.0.31",
    )

    with pytest.raises(ConflictError, match="lazycloud-agent leave"):
        gateway.delete_unit(
            pool_machine.pool,
            workspace_id=pool_machine.workspace_id,
        )
    _leave(gateway, pool_machine)
    gateway.delete_unit(pool_machine.pool, workspace_id=pool_machine.workspace_id)
    assert control.removed_device_ids == ["machine-device", "pool-device"]
    assert control.revoked_key_ids == [machine_key_id, pool_key_id]


def test_cleanup_revokes_platform_authority_before_retryable_device_removal(
    isolated_services: ApiServices,
) -> None:
    control = _RecordingTailnetControl()
    gateway = _gateway(isolated_services, control, key_prefix="tailnet-cleanup-retry")
    enrolled = _enroll(isolated_services, gateway, pool="cleanup-retry")
    _, expected_hostname = _issue(gateway, enrolled)
    _bind(
        gateway,
        control,
        enrolled,
        node_id="retry-node",
        device_id="retry-device",
        hostname=expected_hostname,
        address="100.64.0.40",
    )
    control.remove_error = TailnetControlError(
        TailnetControlErrorCode.UpstreamUnavailable,
        "coordination service unavailable",
        retryable=True,
    )

    _leave(gateway, enrolled)

    assert not gateway.stream_agent(StreamAgentRequest(agent_token=enrolled.agent_token)).ok
    store = DatabaseTailnetCleanupStore(isolated_services.context)
    tombstone = store.get_by_machine(enrolled.machine_id)
    assert tombstone is not None
    with isolated_services.context.database.session() as session:
        enrollment = ComputeMachineEnrollmentRepository(session).by_machine(
            enrolled.workspace_id,
            enrolled.machine_id,
            pool=enrolled.pool,
        )
        machine = MachineRepository(session).get_across_workspaces(enrolled.machine_id)
    assert enrollment is None
    assert machine is None

    control.remove_error = None
    batch = TailnetCleanupCoordinator(store, control).reconcile_due(
        now=tombstone.not_before,
    )
    assert batch.completed_count == 1
    assert store.get_by_machine(enrolled.machine_id) is None
