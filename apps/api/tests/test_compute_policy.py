from __future__ import annotations

from contextlib import ExitStack
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from database.repositories.aws_connections import AwsAccountConnectionRepository
from database.repositories.billing import BillingAccountRepository
from database.repositories.compute import (
    ComputeMachineEnrollmentCreate,
    ComputeMachineEnrollmentRepository,
    ComputeProviderInstanceRecord,
    ComputeProviderInstanceRepository,
    ComputeUnitRepository,
)
from database.repositories.identity import WorkspaceRepository
from database.repositories.orchestration import MachineRepository
from fastapi.testclient import TestClient
from identity.auth import TokenIssuer
from shared.aws_connections import (
    AwsAccountAuthorizationGeneration,
    AwsAccountAuthorizationMode,
    AwsAccountAuthorizationPhase,
    AwsAccountConnection,
    AwsAccountConnectionPhase,
)
from shared.billing_accounts import BillingAccountStatus
from shared.billing_plans import BillingPlanId, SubscriptionTermsVersion
from shared.capacity import CapacityOwnerKind, CapacityOwnerSource
from shared.compute_enrollment import MachineReadinessPhase
from shared.compute_fleet import Machine, MachineLifecycle
from shared.compute_policy import (
    ComputeCapacityMode,
    ComputeUnitRecord,
    ComputeUnitVisibility,
    UnitName,
)
from shared.deployment_records import DeploymentSpec
from shared.errors import InvalidInputError
from shared.http.compute import UnitMachineListResponse
from shared.http.compute_policy import (
    ConnectionMachineListResponse,
    WorkspaceComputeSummaryResponse,
)
from shared.identity import WorkspaceStatus
from shared.placement import Placement
from shared.supplier_costs import SupplierCostTerms
from tests.workspaces import workspace_owner_user_id


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


def test_connection_machines_list_by_placement_and_summary_counts_by_lifecycle(
    isolated_services: ApiServices,
    request: pytest.FixtureRequest,
) -> None:
    """The cloud card and the self-hosted panel split on placement kind, not provider name.

    A node the connected cloud launched enrolls through the same agent as a
    joined host, so the provider column says "agent" for both. The row's
    placement is what says whose it is.
    """
    workspace_id = _workspace_id(isolated_services)
    owner_id = _workspace_owner_id(isolated_services)
    client = _account_client(isolated_services, request, user_id=owner_id)
    _seed_ready_aws_connection(isolated_services)
    with isolated_services.context.database.session() as session:
        connection = AwsAccountConnectionRepository(session).get_for_workspace_owner(workspace_id)
    assert connection is not None
    pool_id = str(uuid4())
    now = datetime.now(UTC)
    ready_machine_id = str(uuid4())
    pending_machine_id = str(uuid4())
    leaving_machine_id = str(uuid4())
    gone_machine_id = str(uuid4())
    joined_machine_id = str(uuid4())
    with isolated_services.context.database.session() as session:
        ComputeUnitRepository(session).upsert(
            ComputeUnitRecord(
                id=pool_id,
                capacity_owner_id=pool_id,
                capacity_owner_kind=CapacityOwnerKind.PooledProvider,
                capacity_owner_source=CapacityOwnerSource.Provider,
                workspace_id=workspace_id,
                name=UnitName("current-aws-inventory"),
                placement=connection.placement,
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
        machines = MachineRepository(session)
        for machine_id, lifecycle in (
            (ready_machine_id, MachineLifecycle.Ready),
            (pending_machine_id, MachineLifecycle.Provisioning),
            (leaving_machine_id, MachineLifecycle.Terminating),
            (gone_machine_id, MachineLifecycle.Deleted),
        ):
            machines.upsert(
                Machine(
                    id=machine_id,
                    placement=connection.placement,
                    capacity_owner_id=pool_id,
                    provider="agent",
                    lifecycle=lifecycle,
                    lifecycle_at=now,
                    cpu=4.0,
                    memory="32768Mi",
                    created_at=now,
                    updated_at=now,
                ),
                workspace_id=workspace_id,
            )
        machines.upsert(
            Machine(
                id=joined_machine_id,
                name="rack-1",
                workspace_ids=(workspace_id,),
                placement=Placement.machine(joined_machine_id),
                capacity_owner_id=str(uuid4()),
                provider="agent",
                lifecycle=MachineLifecycle.Joining,
                lifecycle_at=now,
                created_at=now,
                updated_at=now,
            ),
            workspace_id=workspace_id,
            owner_user_id=owner_id,
        )
        ComputeMachineEnrollmentRepository(session).create(
            ComputeMachineEnrollmentCreate(
                user_id=owner_id,
                workspace_id=workspace_id,
                capacity_owner_id=pool_id,
                placement=connection.placement,
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
        instances = ComputeProviderInstanceRepository(session)
        for machine_id, status, hourly_cost_micros in (
            (ready_machine_id, "active", 100_000),
            (pending_machine_id, "pending", 200_000),
            (leaving_machine_id, "terminating", 300_000),
            (gone_machine_id, "deleted", 400_000),
        ):
            instances.upsert(
                ComputeProviderInstanceRecord(
                    id=str(uuid4()),
                    provider=f"aws:{connection.id}",
                    offer_id="us-east-1:m7i.xlarge",
                    status=status,
                    source="pooled",
                    pool_id=pool_id,
                    instance_type="m7i.xlarge",
                    instance_id=f"i-{uuid4().hex[:17]}",
                    machine_id=machine_id,
                    region="us-east-1",
                    availability_zone="us-east-1a",
                    cost_terms=SupplierCostTerms(
                        compute_hourly_micros=hourly_cost_micros,
                        root_disk_hourly_micros=0,
                        public_ipv4_hourly_micros=0,
                    ),
                )
            )

    summary_response = client.get("/api/v1/compute/summary")
    assert summary_response.status_code == 200
    summary = WorkspaceComputeSummaryResponse.model_validate_json(summary_response.content)
    assert summary.instances.total == 3
    assert summary.instances.ready == 1
    assert summary.instances.pending == 1
    assert summary.instances.degraded == 1
    assert summary.cost.hourly_micros == 600_000

    instances_response = client.get("/api/v1/compute/instances")
    assert instances_response.status_code == 200
    cloud = ConnectionMachineListResponse.model_validate_json(instances_response.content)
    assert {(item.id, item.lifecycle) for item in cloud.data} == {
        (ready_machine_id, MachineLifecycle.Ready),
        (pending_machine_id, MachineLifecycle.Provisioning),
        (leaving_machine_id, MachineLifecycle.Terminating),
    }
    ready = next(item for item in cloud.data if item.id == ready_machine_id)
    assert ready.connected
    assert ready.placement == connection.placement
    assert ready.instance_type == "m7i.xlarge"
    assert ready.availability_zone == "us-east-1a"
    assert ready.cpu_millicores == 4_000
    assert ready.memory_mb == 32_768

    self_hosted_response = client.get("/api/v1/machines/self-hosted")
    assert self_hosted_response.status_code == 200
    self_hosted = UnitMachineListResponse.model_validate_json(self_hosted_response.content)
    assert [(item.id, item.name, item.lifecycle) for item in self_hosted.data] == [
        (joined_machine_id, "rack-1", MachineLifecycle.Joining)
    ]

    # A machine that stops reporting keeps its phase; the view says it is not connected.
    with isolated_services.context.database.session() as session:
        enrollments = ComputeMachineEnrollmentRepository(session)
        enrollment = enrollments.by_machine(workspace_id, ready_machine_id, for_update=True)
        assert enrollment is not None
        enrollments.save(
            enrollment.model_copy(
                update={
                    "readiness_phase": MachineReadinessPhase.Offline,
                    "last_disconnect_at": datetime.now(UTC),
                    "updated_at": datetime.now(UTC),
                }
            )
        )
    offline = next(
        item
        for item in ConnectionMachineListResponse.model_validate_json(
            client.get("/api/v1/compute/instances").content
        ).data
        if item.id == ready_machine_id
    )
    assert offline.lifecycle is MachineLifecycle.Ready
    assert not offline.connected

    with isolated_services.context.database.session() as session:
        workspaces = WorkspaceRepository(session)
        workspace = workspaces.get(workspace_id)
        assert workspace is not None
        workspaces.upsert(workspace.model_copy(update={"status": WorkspaceStatus.Deleting}))
    # The account still sees its cloud while a workspace drains; the workspace summary does not.
    assert client.get("/api/v1/compute/instances").status_code == 200
    assert (
        client.get("/api/v1/compute/summary", params={"workspace": workspace_id}).status_code == 404
    )


def test_deployment_refuses_a_machine_that_is_unknown_or_serves_another_workspace(
    isolated_services: ApiServices,
) -> None:
    """Naming a machine pins the deployment to it or fails; nothing falls back.

    The refusal carries the name so the caller can see which machine they
    typed, and a machine that exists but serves another workspace refuses the
    same way: for this workspace it does not exist.
    """
    workspace_id = _workspace_id(isolated_services)
    owner_id = _workspace_owner_id(isolated_services)
    elsewhere = isolated_services.control_plane_service.set_workspace(
        "machine-elsewhere", owner_user_id=owner_id
    )
    machine_id = str(uuid4())
    with isolated_services.context.database.session() as session:
        MachineRepository(session).upsert(
            Machine(
                id=machine_id,
                name="rack-7",
                workspace_ids=(elsewhere.id,),
                placement=Placement.machine(machine_id),
                provider="agent",
                lifecycle=MachineLifecycle.Ready,
            ),
            workspace_id=elsewhere.id,
        )

    with pytest.raises(InvalidInputError, match="'no-such-machine'"):
        isolated_services.deployments.deploy(
            DeploymentSpec(name="on-missing", metadata={"machine": "no-such-machine"}),
            workspace="default",
        )
    with pytest.raises(InvalidInputError, match="'rack-7'"):
        isolated_services.deployments.deploy(
            DeploymentSpec(name="on-foreign", metadata={"machine": "rack-7"}),
            workspace="default",
        )
    pinned = isolated_services.deployments.deploy(
        DeploymentSpec(name="on-rack", metadata={"machine": "rack-7"}),
        workspace=elsewhere.id,
    )
    located = isolated_services.deployments.deploy(
        DeploymentSpec(name="located"), workspace="default"
    )

    assert (pinned.placement, pinned.machine) == (Placement.machine(machine_id), "rack-7")
    assert (located.placement, located.machine) == (Placement.platform(), "")
    assert workspace_id != elsewhere.id


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
            plan=BillingPlanId.Business,
            subscription_terms_version=SubscriptionTermsVersion.Business,
            scheduled_terms_version=None,
            scheduled_change_at=None,
        )
        AwsAccountConnectionRepository(session).create(
            AwsAccountConnection(
                id=str(uuid4()),
                user_id=owner_id,
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
    return owner_id
