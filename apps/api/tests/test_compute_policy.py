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
from control.service import ControlPlaneService
from database.repositories.billing import BillingAccountRepository
from database.repositories.compute import (
    AwsAccountConnectionRepository,
    ComputeMachineEnrollmentCreate,
    ComputeMachineEnrollmentRepository,
    ComputeProviderInstanceRecord,
    ComputeProviderInstanceRepository,
    ComputeUnitRepository,
)
from database.repositories.orchestration import MachineRepository, WorkerRepository
from fastapi.testclient import TestClient
from gateway.settings import GatewaySettings
from identity.auth import AuthService, TokenIssuer
from networking.settings import BackendRouteSettings
from provider_aws import aws_account_connection_template_identity
from provider_clients.settings import AwsAccountConnectionSettings, AwsCapacitySettings
from provider_pangolin import PangolinSettings
from pydantic import SecretStr
from scheduler.compute_hooks import SchedulerComputeHooks
from scheduler.state import RedisSchedulerWorkerRepository
from shared.aws_connections import (
    AwsAccountAuthorizationGeneration,
    AwsAccountAuthorizationMode,
    AwsAccountAuthorizationPhase,
    AwsAccountComputeConfiguration,
    AwsAccountConnection,
    AwsAccountConnectionPhase,
)
from shared.billing_accounts import BillingAccountStatus
from shared.billing_plans import BillingPlanId
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
    ComputeResourceRequirements,
    ComputeUnitRecord,
    ComputeUnitVisibility,
    MachinePool,
    UnitName,
)
from shared.deployment_records import DeploymentSpec
from shared.http.aws_connections import AwsConnectionCurrentResponse, AwsConnectionResponse
from shared.http.compute_policy import (
    MachinePoolListResponse,
    WorkspaceComputeInstanceListResponse,
    WorkspaceComputeSummaryResponse,
)
from shared.identity import TokenKind
from shared.scheduling import SchedulerWorkerRecord, SchedulerWorkerStatus
from storage.workspace_storage_issuers import StoredWorkspaceStorageIssuer
from tests.service_fixtures import owned_workspace, workspace_owner_user_id
from tests.url_constants import EXAMPLE_COM_URL


def _workspace_owner_id(services: ApiServices) -> str:
    """The account that owns the default workspace; connections hang off it."""
    with services.context.database.session() as session:
        workspace_id = services.context.default_workspace_id(session)
    user_id = workspace_owner_user_id(services.context, workspace_id)
    return user_id


def _account_client(
    services: ApiServices,
    request: pytest.FixtureRequest,
    *,
    user_id: str,
) -> TestClient:
    """A client signed in as a person: cloud capacity is read per account, not per workspace."""
    issuer = TokenIssuer(services.context)
    with services.context.database.session() as session:
        raw_token, _record = issuer.issue_for_user(session, "compute-account", user_id=user_id)
    issuer.committed()
    client_stack = ExitStack()
    request.addfinalizer(client_stack.close)
    return client_stack.enter_context(
        TestClient(
            create_app(services),
            headers={"Authorization": f"Bearer {raw_token}"},
        )
    )


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
            "m7i.large": 100_800,
            "m7i.xlarge": 201_600,
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
    backend_route_settings = BackendRouteSettings(auth_key=SecretStr(uuid4().hex))
    services = ApiServices.create(
        isolated_services.database,
        workspace_storage_issuer=StoredWorkspaceStorageIssuer(),
        root=isolated_services.root,
        create_schema=False,
        gateway_settings=GatewaySettings(
            public_http_url=EXAMPLE_COM_URL,
            runtime_callback_http_url=EXAMPLE_COM_URL,
        ),
        agent_binary_settings=configuration.agent_binaries,
        aws_account_connection_settings=configuration.connection,
        aws_capacity_settings=configuration.capacity,
        pangolin_settings=PangolinSettings(
            api_url="https://pangolin.example.test/v1",
            api_key=SecretStr("integration-key"),
            organization_id="organization-one",
            endpoint="https://pangolin.example.test",
        ),
        backend_route_settings=backend_route_settings,
        volume_filesystem=isolated_services.volume_filesystem,
        redis_client=isolated_services.redis_client,
        binary_redis_client=isolated_services.binary_redis_client,
        owns_redis_client=False,
        owns_binary_redis_client=False,
    )
    request.addfinalizer(services.close)
    return services


def test_account_compute_configuration_rejects_unavailable_catalog_selections(
    isolated_services: ApiServices,
    request: pytest.FixtureRequest,
) -> None:
    owner_id = _seed_ready_aws_connection(isolated_services)
    services = _configured_aws_services(isolated_services, request)
    client = _account_client(services, request, user_id=owner_id)
    current = _current_connection(client)

    unavailable_region = client.put(
        "/api/v1/aws-connection/compute",
        json={
            "expected_revision": current.compute.revision,
            "compute": {
                **current.compute.model_dump(mode="json"),
                "default_region": "eu-west-1",
                "allowed_regions": ["eu-west-1"],
            },
        },
    )
    unavailable_type = client.put(
        "/api/v1/aws-connection/compute",
        json={
            "expected_revision": current.compute.revision,
            "compute": {
                **current.compute.model_dump(mode="json"),
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
    owner_id = _seed_ready_aws_connection(isolated_services)
    with isolated_services.context.database.session() as session:
        connection = AwsAccountConnectionRepository(session).get_for_workspace_owner(workspace_id)
    assert connection is not None
    pool_id = str(uuid4())
    ready_machine_id = str(uuid4())
    now = datetime.now(UTC)
    with isolated_services.context.database.session() as session:
        ComputeUnitRepository(session).upsert(
            ComputeUnitRecord(
                id=pool_id,
                capacity_owner_id=pool_id,
                capacity_owner_kind=CapacityOwnerKind.PooledProvider,
                capacity_owner_source=CapacityOwnerSource.Provider,
                workspace_id=workspace_id,
                name=UnitName("current-aws-inventory"),
                pool=MachinePool("aws"),
                provider_ref=f"aws:{connection.id}",
                provider_connection_id=connection.id,
                capacity_mode=ComputeCapacityMode.Pooled,
                visibility=ComputeUnitVisibility.Internal,
                region="us-east-1",
                offer_id="us-east-1:m7i.xlarge",
                capability_key="aws:us-east-1:m7i.xlarge:amd64:runsc",
                max_machines=3,
            )
        )
        MachineRepository(session).upsert(
            Machine(
                id=ready_machine_id,
                pool=MachinePool("aws"),
                provider="aws",
                status=ResourceStatus.Running,
                created_at=now,
                updated_at=now,
            ),
            workspace_id=workspace_id,
        )
        ComputeMachineEnrollmentRepository(session).create(
            ComputeMachineEnrollmentCreate(
                user_id=owner_id,
                workspace_id=workspace_id,
                capacity_owner_id=pool_id,
                pool=MachinePool("aws"),
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
                pool=MachinePool("aws"),
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
                    offer_id="us-east-1:m7i.xlarge",
                    status=status,
                    source="pooled",
                    pool_id=pool_id,
                    instance_type="m7i.xlarge",
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
            pool=MachinePool("aws"),
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            machine_id=ready_machine_id,
            status=SchedulerWorkerStatus.Available,
        )
    )
    client = _account_client(isolated_services, request, user_id=owner_id)
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


def test_account_compute_configuration_is_editable_during_authorization_replacement(
    isolated_services: ApiServices,
    request: pytest.FixtureRequest,
) -> None:
    """Replacing authorization must not freeze the limits the running fleet obeys.

    The connection still hosts workloads while a replacement is completed, so an
    owner who needs to lower a ceiling in that window has to be able to.
    """
    owner_id = _seed_ready_aws_connection(isolated_services, reconnecting=True)
    services = _configured_aws_services(isolated_services, request)
    client = _account_client(services, request, user_id=owner_id)
    current = _current_connection(client)

    response = client.put(
        "/api/v1/aws-connection/compute",
        json={
            "expected_revision": current.compute.revision,
            "compute": {**current.compute.model_dump(mode="json"), "idle_timeout_seconds": 600},
        },
    )

    assert response.status_code == 200, response.text
    updated = AwsConnectionResponse.model_validate_json(response.content)
    assert updated.hosts_workloads
    assert updated.compute.idle_timeout_seconds == 600
    assert updated.compute.revision == current.compute.revision + 1
    assert _current_connection(client).compute.idle_timeout_seconds == 600


def test_placement_names_the_pool_and_leaves_the_unit_to_arbitration(
    isolated_services: ApiServices,
) -> None:
    """Placement resolves a pool; it never picks which unit inside it serves.

    Two units feed one pool here. Placement answering with the pool is what
    leaves the acquisition loop both candidates to fail over between; answering
    with a unit would pin the request to one of them.
    """
    workspace_id = _workspace_id(isolated_services)
    for name, owner in (
        ("unit-a", "10000000-0000-4000-8000-000000000001"),
        ("unit-b", "20000000-0000-4000-8000-000000000002"),
    ):
        isolated_services.compute.create_unit(
            UnitName(name),
            workspace=workspace_id,
            pool=MachinePool("shared-pool"),
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
            requested_pool="shared-pool",
            requirements=ComputeResourceRequirements(cpu_millicores=1_000, memory_mb=1_024),
        )
    )

    assert result.pool == "shared-pool"
    # A unit in the pool already hosts this shape, so nothing is provisioned.
    assert recorder.requests == []


def test_placement_defaults_to_the_platform_pool_without_a_connection(
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

    assert result.pool == LAZYCLOUD_MACHINE_POOL
    # Nothing provisions into a pool no connected account feeds.
    assert recorder.requests == []


def test_machine_pool_listing_is_scoped_to_the_caller_workspace(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    """A caller only sees the pools its own workspace's units feed.

    A pool is derived from the units feeding it rather than stored, so this
    listing is only as scoped as the query behind it: a read across workspaces
    would hand one tenant the names of another tenant's capacity.
    """
    control = ControlPlaneService(isolated_services.context)
    caller = owned_workspace(control, "pool-listing-caller")
    other = owned_workspace(control, "pool-listing-other")
    isolated_services.compute.create_unit(
        UnitName("caller-unit"),
        workspace=caller.id,
        pool=MachinePool("caller-pool"),
        provider="agent",
    )
    isolated_services.compute.create_unit(
        UnitName("other-unit"),
        workspace=other.id,
        pool=MachinePool("other-pool"),
        provider="agent",
    )
    token, _record = AuthService(isolated_services.context).create_token(
        "pool-listing-token",
        kind=TokenKind.Workspace,
        workspace_id=caller.id,
    )
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))

    response = client.get(
        "/api/v1/compute/pools",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    pools = MachinePoolListResponse.model_validate_json(response.content)
    assert [item.name for item in pools.data] == ["caller-pool"]


def test_deployment_placement_is_pinned_when_workspace_default_changes(
    isolated_services: ApiServices,
) -> None:
    _seed_ready_aws_connection(isolated_services)
    policies = WorkspaceComputePolicyService(isolated_services.context)
    workspace_id = _workspace_id(isolated_services)
    original = isolated_services.deployments.deploy(
        DeploymentSpec(name="before-policy-change"),
        workspace="default",
    )
    policy = policies.get_policy(workspace="default")

    policies.update_policy(
        workspace="default",
        expected_revision=policy.revision,
        default_pool="aws",
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

    assert persisted_original.pool == LAZYCLOUD_MACHINE_POOL
    assert scheduled_original.pool == LAZYCLOUD_MACHINE_POOL
    assert created_after.pool == "aws"


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
    ) -> ComputeUnitRecord:
        del root_volume_gib, idle_timeout_seconds, allowed_instance_types
        self.requests.append(requirements)
        return ComputeUnitRecord(
            id="11111111-1111-4111-8111-111111111111",
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            capacity_owner_kind=CapacityOwnerKind.PooledProvider,
            capacity_owner_source=CapacityOwnerSource.Provider,
            workspace_id=workspace,
            name=UnitName("internal-aws-cpu"),
            pool=MachinePool("aws"),
            provider_ref="aws:22222222-2222-4222-8222-222222222222",
            provider_connection_id="22222222-2222-4222-8222-222222222222",
            capacity_mode=ComputeCapacityMode.Pooled,
            visibility=ComputeUnitVisibility.Internal,
            region=region,
            offer_id="us-east-1:m7i.xlarge",
            capability_key="aws:us-east-1:m7i.xlarge:amd64:runsc",
            desired_machines=desired_machines,
            max_machines=workspace_machine_limit,
        )


def _current_connection(client: TestClient) -> AwsConnectionResponse:
    response = client.get("/api/v1/aws-connection")
    assert response.status_code == 200, response.text
    current = AwsConnectionCurrentResponse.model_validate_json(response.content)
    assert current.connection is not None
    return current.connection


def _workspace_id(isolated_services: ApiServices) -> str:
    with isolated_services.context.database.session() as session:
        return isolated_services.context.default_workspace_id(session)


def _seed_ready_aws_connection(
    isolated_services: ApiServices, *, reconnecting: bool = False
) -> str:
    now = datetime.now(UTC)
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
    owner_id = _workspace_owner_id(isolated_services)
    with isolated_services.context.database.session() as session:
        BillingAccountRepository(session).upsert(
            user_id=owner_id,
            status=BillingAccountStatus.Active,
            provider_customer_id=f"cus_fixture_{owner_id}",
            provider_subscription_id=f"sub_fixture_{owner_id}",
            provider_credit_grant_id=f"credgr_fixture_{owner_id}",
            plan=BillingPlanId.Team,
        )
        AwsAccountConnectionRepository(session).create(
            AwsAccountConnection(
                id=str(uuid4()),
                user_id=owner_id,
                account_id=account_id,
                external_id="x" * 48,
                pool=MachinePool("aws"),
                # No warm baseline: control-plane startup reconciles what every
                # connected account asks for, and none of these tests are about
                # provisioning it.
                compute=AwsAccountComputeConfiguration(
                    initial_cpu_workers=0,
                    min_cpu_workers=0,
                    max_cpu_instances=0,
                ),
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
    return owner_id
