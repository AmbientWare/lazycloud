from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from agent.binary import AgentBinarySettings
from api.fastapi_app import create_app
from api.server.services import ApiServices
from compute.agent_control import agent_machine_worker_id
from compute.policy import WorkspaceComputePolicyService
from compute.request_placement import (
    ComputeCapacityPlacementRequest,
    ComputeCapacityPlacementService,
)
from database.repositories.compute import (
    AwsAccountConnectionRepository,
    ComputeMachineEnrollmentCreate,
    ComputeMachineEnrollmentRepository,
    ComputePoolRepository,
    ComputeProviderInstanceRecord,
    ComputeProviderInstanceRepository,
)
from database.repositories.orchestration import MachineRepository, WorkerRepository
from fastapi.testclient import TestClient
from gateway.settings import GatewaySettings
from identity.auth import AuthService
from networking.settings import (
    BackendRouteSettings,
    TailnetControlSettings,
    TailnetRuntimeSettings,
)
from networking.tailnet import TailnetRuntimeMode
from provider_aws import aws_account_connection_template_identity
from provider_clients import configured_aws_compute_catalog
from provider_clients.settings import AwsAccountConnectionSettings, AwsCapacitySettings
from pydantic import SecretStr
from scheduler.compute_hooks import SchedulerComputeHooks
from scheduler.state import RedisSchedulerWorkerRepository
from shared.aws_connections import (
    AwsAccountAuthorizationGeneration,
    AwsAccountAuthorizationMode,
    AwsAccountAuthorizationPhase,
    AwsAccountConnection,
    AwsAccountConnectionPhase,
)
from shared.capacity import CapacityOwnerKind, CapacityOwnerSource
from shared.compute_enrollment import (
    MachineBootstrapPhase,
    MachineReadinessPhase,
    MachineServiceState,
)
from shared.compute_fleet import Machine, ResourceStatus, Worker
from shared.compute_policy import (
    LAZYCLOUD_MACHINE_POOL,
    ComputeCapacityMode,
    ComputePlacementTarget,
    ComputePoolRecord,
    ComputePoolVisibility,
    ComputeResourceRequirements,
)
from shared.deployment_records import DeploymentSpec
from shared.http.compute_policy import (
    WorkspaceComputeInstanceListResponse,
    WorkspaceComputePolicyResponse,
    WorkspaceComputeSummaryResponse,
)
from shared.scheduling import SchedulerWorkerRecord, SchedulerWorkerStatus
from tests.url_constants import EXAMPLE_COM_URL


def _client(
    isolated_services: ApiServices,
    request: pytest.FixtureRequest,
) -> TestClient:
    token, _record = AuthService(isolated_services.context).create_token("compute-policy")
    client_stack = ExitStack()
    request.addfinalizer(client_stack.close)
    client = client_stack.enter_context(
        TestClient(
            create_app(isolated_services),
            headers={"Authorization": f"Bearer {token}"},
        )
    )
    return client


@dataclass(frozen=True, slots=True)
class _AwsCatalogConfiguration:
    agent_binaries: AgentBinarySettings
    connection: AwsAccountConnectionSettings
    capacity: AwsCapacitySettings


def _aws_catalog_configuration() -> _AwsCatalogConfiguration:
    agent_binary_settings = AgentBinarySettings(
        binary_dir=Path("/tmp/agent-binarys"),
        binary_version="0.1.0",
        binary_sha256_by_arch={"amd64": "a" * 64},
    )
    template_identity = aws_account_connection_template_identity()
    aws_account_connection_settings = AwsAccountConnectionSettings(
        enabled=True,
        template_url=(
            "https://assets.s3.us-east-1.amazonaws.com/templates/"
            f"{template_identity.sha256}/connection.json"
        ),
        control_principal_arn="arn:aws:iam::123456789012:role/control-plane",
    )
    aws_capacity_settings = AwsCapacitySettings(
        worker_image_digest=f"worker@sha256:{'b' * 64}",
        agent_binary_url=(
            f"https://s3.us-east-1.amazonaws.com/releases/agents/0.1.0/{'b' * 64}/"
            "lazycloud-agent-linux-amd64"
        ),
        cpu_ami_ids={
            "us-west-2": "ami-0123456789abcdef0",
            "us-east-1": "ami-1234567890abcdef0",
        },
        gpu_ami_ids={
            "us-west-2": "ami-2345678901abcdef0",
        },
        instance_hourly_micros={
            "g6.xlarge": 804_000,
            "i4i.xlarge": 340_000,
        },
    )
    return _AwsCatalogConfiguration(
        agent_binaries=agent_binary_settings,
        connection=aws_account_connection_settings,
        capacity=aws_capacity_settings,
    )


def _configured_aws_services(
    isolated_services: ApiServices,
    request: pytest.FixtureRequest,
) -> ApiServices:
    configuration = _aws_catalog_configuration()
    tailnet_runtime_settings = TailnetRuntimeSettings(
        mode=TailnetRuntimeMode.Sidecar,
    )
    tailnet_control_settings = TailnetControlSettings(
        oauth_client_id=str(uuid4()),
        oauth_client_secret=SecretStr(uuid4().hex),
    )
    backend_route_settings = BackendRouteSettings(auth_key=SecretStr(uuid4().hex))
    services = ApiServices.create(
        isolated_services.database,
        root=isolated_services.root,
        create_schema=False,
        gateway_settings=GatewaySettings(
            public_http_url=EXAMPLE_COM_URL,
            runtime_callback_http_url=EXAMPLE_COM_URL,
        ),
        agent_binary_settings=configuration.agent_binaries,
        aws_account_connection_settings=configuration.connection,
        aws_capacity_settings=configuration.capacity,
        tailnet_runtime_settings=tailnet_runtime_settings,
        tailnet_control_settings=tailnet_control_settings,
        backend_route_settings=backend_route_settings,
        volume_filesystem=isolated_services.volume_filesystem,
        redis_client=isolated_services.redis_client,
        binary_redis_client=isolated_services.binary_redis_client,
        owns_redis_client=False,
        owns_binary_redis_client=False,
    )
    request.addfinalizer(services.close)
    return services


def test_workspace_policy_rejects_unavailable_catalog_selections(
    isolated_services: ApiServices,
    request: pytest.FixtureRequest,
) -> None:
    services = _configured_aws_services(isolated_services, request)
    client = _client(services, request)
    policy_response = client.get("/api/v1/compute/policy")
    policy = WorkspaceComputePolicyResponse.model_validate_json(policy_response.content)

    unavailable_region = client.put(
        "/api/v1/compute/policy",
        json={
            "expected_revision": policy.revision,
            "default_placement": "managed",
            "aws": {
                **policy.aws.model_dump(mode="json"),
                "default_region": "eu-west-1",
                "allowed_regions": ["eu-west-1"],
            },
        },
    )
    unavailable_type = client.put(
        "/api/v1/compute/policy",
        json={
            "expected_revision": policy.revision,
            "default_placement": "managed",
            "aws": {
                **policy.aws.model_dump(mode="json"),
                "default_region": "us-west-2",
                "default_instance_type": "g5.xlarge",
                "allowed_regions": ["us-west-2"],
                "allowed_instance_types": ["g5.xlarge"],
            },
        },
    )

    assert unavailable_region.status_code == 400
    assert unavailable_region.json()["detail"] == (
        "AWS regions are not available for configured capacity: eu-west-1"
    )
    assert unavailable_type.status_code == 400
    assert unavailable_type.json()["detail"] == (
        "AWS instance types are not available in allowed regions: g5.xlarge"
    )


def test_compute_inventory_excludes_terminal_history_and_classifies_open_capacity(
    isolated_services: ApiServices,
    request: pytest.FixtureRequest,
) -> None:
    workspace_id = _workspace_id(isolated_services)
    _seed_ready_aws_connection(isolated_services)
    with isolated_services.context.database.session() as session:
        connection = AwsAccountConnectionRepository(session).get_for_workspace(workspace_id)
    assert connection is not None
    pool_id = str(uuid4())
    ready_machine_id = str(uuid4())
    now = datetime.now(UTC)
    with isolated_services.context.database.session() as session:
        ComputePoolRepository(session).upsert(
            ComputePoolRecord(
                id=pool_id,
                capacity_owner_id=pool_id,
                capacity_owner_kind=CapacityOwnerKind.PooledProvider,
                capacity_owner_source=CapacityOwnerSource.Provider,
                workspace_id=workspace_id,
                name="current-aws-inventory",
                machine_pool="aws",
                provider_ref=f"aws:{connection.id}",
                provider_connection_id=connection.id,
                capacity_mode=ComputeCapacityMode.Pooled,
                visibility=ComputePoolVisibility.Internal,
                region="us-east-1",
                offer_id="us-east-1:i4i.xlarge",
                capability_key="aws:us-east-1:i4i.xlarge:amd64:runc",
                max_machines=3,
            )
        )
        MachineRepository(session).upsert(
            Machine(
                id=ready_machine_id,
                pool="current-aws-inventory",
                provider="aws",
                status=ResourceStatus.Running,
                created_at=now,
                updated_at=now,
            ),
            workspace_id=workspace_id,
        )
        ComputeMachineEnrollmentRepository(session).create(
            ComputeMachineEnrollmentCreate(
                workspace_id=workspace_id,
                capacity_owner_id=pool_id,
                pool_name="current-aws-inventory",
                machine_id=ready_machine_id,
                machine_fingerprint_hash="f" * 64,
                credential_hash="c" * 64,
                preflight_passed=True,
                heartbeat_confirmed=True,
                schedulable=True,
                readiness_phase=MachineReadinessPhase.Ready,
                last_join_at=now,
                last_heartbeat_at=now,
            )
        )
        WorkerRepository(session).upsert(
            Worker(
                id=agent_machine_worker_id(ready_machine_id),
                machine_id=ready_machine_id,
                pool="current-aws-inventory",
                status=ResourceStatus.Running,
                last_seen_at=now,
                created_at=now,
            ),
            workspace_id=workspace_id,
        )
        instances = ComputeProviderInstanceRepository(session)
        for status, hourly_cost_micros in (
            ("active", 100_000),
            ("pending", 200_000),
            ("terminating", 300_000),
            ("deleted", 400_000),
            ("failed", 500_000),
        ):
            instances.upsert(
                ComputeProviderInstanceRecord(
                    id=str(uuid4()),
                    provider="aws",
                    offer_id="us-east-1:i4i.xlarge",
                    status=status,
                    source="pooled",
                    pool_id=pool_id,
                    instance_type="i4i.xlarge",
                    instance_id=f"i-{uuid4().hex[:17]}",
                    machine_id=ready_machine_id if status == "active" else None,
                    hourly_cost_micros=hourly_cost_micros,
                )
            )

    hooks = isolated_services.compute.scheduler_hooks
    assert isinstance(hooks, SchedulerComputeHooks)
    workers = hooks.workers
    assert isinstance(workers, RedisSchedulerWorkerRepository)
    workers.add_worker(
        SchedulerWorkerRecord(
            worker_id=agent_machine_worker_id(ready_machine_id),
            pool_name="current-aws-inventory",
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            machine_id=ready_machine_id,
            status=SchedulerWorkerStatus.Available,
        )
    )
    client = _client(isolated_services, request)
    summary_response = client.get("/api/v1/compute/summary")
    instances_response = client.get("/api/v1/compute/instances")

    assert summary_response.status_code == 200
    summary = WorkspaceComputeSummaryResponse.model_validate_json(summary_response.content)
    assert summary.instances.total == 3
    assert summary.instances.ready == 1
    assert summary.instances.pending == 1
    assert summary.instances.degraded == 1
    assert summary.cost.hourly_micros == 600_000
    assert instances_response.status_code == 200
    current = WorkspaceComputeInstanceListResponse.model_validate_json(instances_response.content)
    # `status` reports the platform's verdict, not what the node claimed:
    # a machine serves because its worker takes work.
    assert {item.status for item in current.data} == {
        "serving",
        "provisioning",
        "deleting",
    }
    serving = next(item for item in current.data if item.status == "serving")
    assert serving.service_state is MachineServiceState.Serving
    assert serving.bootstrap_phase is not MachineBootstrapPhase.Failed

    with isolated_services.context.database.session() as session:
        instances = ComputeProviderInstanceRepository(session)
        for record in instances.list_for_pool(pool_id):
            instances.upsert(record.model_copy(update={"status": "deleted"}))
        assert len(instances.list_for_pool(pool_id)) == 5

    zero_summary = WorkspaceComputeSummaryResponse.model_validate_json(
        client.get("/api/v1/compute/summary").content
    )
    zero_inventory = WorkspaceComputeInstanceListResponse.model_validate_json(
        client.get("/api/v1/compute/instances").content
    )
    assert zero_summary.instances.total == 0
    assert zero_summary.cost.hourly_micros == 0
    assert zero_inventory.data == []


def test_policy_rejects_aws_default_without_ready_connection(
    isolated_services: ApiServices,
    request: pytest.FixtureRequest,
) -> None:
    services = _configured_aws_services(isolated_services, request)
    client = _client(services, request)
    policy_response = client.get("/api/v1/compute/policy")
    policy = WorkspaceComputePolicyResponse.model_validate_json(policy_response.content)

    response = client.put(
        "/api/v1/compute/policy",
        json={
            "expected_revision": policy.revision,
            "default_placement": "aws",
            "aws": policy.aws.model_dump(mode="json"),
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "connect and validate AWS before making it the default placement"
    )


def test_policy_accepts_placement_during_authorization_replacement(
    isolated_services: ApiServices,
) -> None:
    configuration = _aws_catalog_configuration()
    _seed_ready_aws_connection(isolated_services, reconnecting=True)
    policies = WorkspaceComputePolicyService(
        isolated_services.context,
        available_catalog=configured_aws_compute_catalog(
            configuration.capacity,
            configuration.agent_binaries,
        ),
    )
    current = policies.get_policy(workspace="default")

    updated = policies.update_policy(
        workspace="default",
        expected_revision=current.revision,
        default_placement=ComputePlacementTarget.Aws,
        aws=current.aws,
    )
    placement = policies.resolve_placement(
        workspace="default",
        requested=ComputePlacementTarget.Aws,
    )

    assert updated.default_placement is ComputePlacementTarget.Aws
    assert placement.target is ComputePlacementTarget.Aws
    assert placement.provider_ref.startswith("aws:")


def test_placement_names_the_group_and_leaves_the_unit_to_arbitration(
    isolated_services: ApiServices,
) -> None:
    """Placement resolves a group; it never picks which unit inside it serves.

    Two units feed one group here. Placement answering with the group is what
    leaves the acquisition loop both candidates to fail over between; answering
    with a unit would pin the request to one of them.
    """
    workspace_id = _workspace_id(isolated_services)
    for name, owner in (
        ("unit-a", "10000000-0000-4000-8000-000000000001"),
        ("unit-b", "20000000-0000-4000-8000-000000000002"),
    ):
        isolated_services.compute.create_pool(
            name,
            workspace=workspace_id,
            machine_pool="shared-group",
            provider="agent",
            capacity_owner_id=owner,
            worker_cpu_millicores=4_000,
            worker_memory_mib=8_192,
        )
    recorder = _RecordingPooledCapacity()
    placement = ComputeCapacityPlacementService(
        isolated_services.context,
        WorkspaceComputePolicyService(isolated_services.context),
        recorder,
    )

    result = placement.place(
        ComputeCapacityPlacementRequest(
            workspace_id=workspace_id,
            requested_pool="shared-group",
            requirements=ComputeResourceRequirements(cpu_millicores=1_000, memory_mb=1_024),
        )
    )

    assert result.machine_pool == "shared-group"
    # A unit in the group already hosts this shape, so nothing is provisioned.
    assert recorder.requests == []


def test_placement_defaults_to_the_platform_group_without_a_connection(
    isolated_services: ApiServices,
) -> None:
    recorder = _RecordingPooledCapacity()
    placement = ComputeCapacityPlacementService(
        isolated_services.context,
        WorkspaceComputePolicyService(isolated_services.context),
        recorder,
    )

    result = placement.place(
        ComputeCapacityPlacementRequest(
            workspace_id=_workspace_id(isolated_services),
            requirements=ComputeResourceRequirements(cpu_millicores=1_000, memory_mb=1_024),
        )
    )

    assert result.machine_pool == LAZYCLOUD_MACHINE_POOL
    # Nothing provisions into a group no connected account feeds.
    assert recorder.requests == []


def test_deployment_placement_is_pinned_when_workspace_default_changes(
    isolated_services: ApiServices,
) -> None:
    configuration = _aws_catalog_configuration()
    _seed_ready_aws_connection(isolated_services)
    policies = WorkspaceComputePolicyService(
        isolated_services.context,
        available_catalog=configured_aws_compute_catalog(
            configuration.capacity,
            configuration.agent_binaries,
        ),
    )
    workspace_id = _workspace_id(isolated_services)
    original = isolated_services.deployments.deploy(
        DeploymentSpec(name="before-policy-change"),
        workspace="default",
    )
    policy = policies.get_policy(workspace="default")

    policies.update_policy(
        workspace="default",
        expected_revision=policy.revision,
        default_placement=ComputePlacementTarget.Aws,
        aws=policy.aws,
    )
    created_after = isolated_services.deployments.deploy(
        DeploymentSpec(name="after-policy-change"),
        workspace="default",
    )
    persisted_original = isolated_services.deployments.get(original.id)
    scheduled_original = ComputeCapacityPlacementService(
        isolated_services.context,
        policies,
        isolated_services.compute,
    ).place(
        ComputeCapacityPlacementRequest(
            workspace_id=workspace_id,
            deployment_id=original.id,
            requirements=ComputeResourceRequirements(cpu_millicores=1_000, memory_mb=1_024),
        )
    )

    assert persisted_original.resolved_placement.target is ComputePlacementTarget.Managed
    assert scheduled_original.machine_pool == LAZYCLOUD_MACHINE_POOL
    assert created_after.resolved_placement.target is ComputePlacementTarget.Aws
    assert created_after.resolved_placement.region == "us-east-1"


@dataclass(slots=True)
class _RecordingPooledCapacity:
    requests: list[ComputeResourceRequirements] = field(default_factory=list)

    def prepare_pooled_capacity(
        self,
        *,
        workspace: str,
        requirements: ComputeResourceRequirements,
        region: str,
        desired_machines: int,
        workspace_machine_limit: int,
        root_volume_gib: int,
        idle_timeout_seconds: int = 300,
        allowed_instance_types: tuple[str, ...] = (),
    ) -> ComputePoolRecord:
        del root_volume_gib, idle_timeout_seconds, allowed_instance_types
        self.requests.append(requirements)
        return ComputePoolRecord(
            id="11111111-1111-4111-8111-111111111111",
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            capacity_owner_kind=CapacityOwnerKind.PooledProvider,
            capacity_owner_source=CapacityOwnerSource.Provider,
            workspace_id=workspace,
            name="internal-aws-cpu",
            machine_pool="aws",
            provider_ref="aws:22222222-2222-4222-8222-222222222222",
            provider_connection_id="22222222-2222-4222-8222-222222222222",
            capacity_mode=ComputeCapacityMode.Pooled,
            visibility=ComputePoolVisibility.Internal,
            region=region,
            offer_id="us-east-1:i4i.xlarge",
            capability_key="aws:us-east-1:i4i.xlarge:amd64:runc",
            desired_machines=desired_machines,
            max_machines=workspace_machine_limit,
        )


def _workspace_id(isolated_services: ApiServices) -> str:
    with isolated_services.context.database.session() as session:
        return isolated_services.context.default_workspace_id(session)


def _seed_ready_aws_connection(
    isolated_services: ApiServices, *, reconnecting: bool = False
) -> None:
    now = datetime.now(UTC)
    workspace_id = _workspace_id(isolated_services)
    account_id = "123456789012"
    authorization = AwsAccountAuthorizationGeneration(
        id=str(uuid4()),
        generation=1,
        role_arn=f"arn:aws:iam::{account_id}:role/compute-control",
        authorization_mode=AwsAccountAuthorizationMode.ExistingRole,
        phase=AwsAccountAuthorizationPhase.Ready,
        last_validated_at=now,
        created_at=now,
        updated_at=now,
    )
    pending = (
        AwsAccountAuthorizationGeneration(
            id=str(uuid4()),
            generation=2,
            role_arn=f"arn:aws:iam::{account_id}:role/compute-control",
            authorization_mode=AwsAccountAuthorizationMode.ExistingRole,
            phase=AwsAccountAuthorizationPhase.AwaitingAuthorization,
            created_at=now,
            updated_at=now,
        )
        if reconnecting
        else None
    )
    with isolated_services.context.database.session() as session:
        AwsAccountConnectionRepository(session).create(
            AwsAccountConnection(
                id=str(uuid4()),
                workspace_id=workspace_id,
                account_id=account_id,
                external_id="x" * 48,
                phase=(
                    AwsAccountConnectionPhase.ReconnectPending
                    if reconnecting
                    else AwsAccountConnectionPhase.Ready
                ),
                active_authorization=authorization,
                pending_authorization=pending,
                node_role_arn=f"arn:aws:iam::{account_id}:role/compute-node",
                node_instance_profile_arn=(
                    f"arn:aws:iam::{account_id}:instance-profile/compute-node"
                ),
                created_at=now,
                updated_at=now,
            )
        )
