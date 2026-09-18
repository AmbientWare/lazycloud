from __future__ import annotations

from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from compute.agent_control import agent_machine_worker_id
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
from database.repositories.orchestration import MachineRepository, WorkerRepository
from fastapi.testclient import TestClient
from identity.auth import TokenIssuer
from scheduler.compute_hooks import SchedulerComputeHooks
from scheduler.state import RedisSchedulerWorkerRepository
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
from shared.compute_enrollment import (
    MachineBootstrapFailureReason,
    MachineBootstrapPhase,
    MachineReadinessPhase,
    MachineServiceState,
)
from shared.compute_fleet import Machine, ResourceStatus, Worker
from shared.compute_policy import (
    LAZYCLOUD_MACHINE_POOL,
    ComputeCapacityMode,
    ComputeUnitRecord,
    ComputeUnitVisibility,
    MachinePool,
    UnitName,
)
from shared.deployment_records import DeploymentSpec
from shared.errors import InvalidInputError
from shared.http.compute_policy import (
    WorkspaceComputeInstanceListResponse,
    WorkspaceComputeSummaryResponse,
)
from shared.identity import WorkspaceStatus
from shared.scheduling import SchedulerWorkerRecord, SchedulerWorkerStatus
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


def test_compute_inventory_excludes_terminal_history_and_classifies_open_capacity(
    isolated_services: ApiServices,
    request: pytest.FixtureRequest,
) -> None:
    workspace_id = _workspace_id(isolated_services)
    owner_id = _workspace_owner_id(isolated_services)
    client = _account_client(isolated_services, request, user_id=owner_id)
    _seed_ready_aws_connection(isolated_services)
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
                    cost_terms=SupplierCostTerms(
                        compute_hourly_micros=hourly_cost_micros,
                        root_disk_hourly_micros=0,
                        public_ipv4_hourly_micros=0,
                    ),
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
            request_poll_expires_at=now + timedelta(seconds=60),
        )
    )
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
        enrollments = ComputeMachineEnrollmentRepository(session)
        enrollment = enrollments.by_machine(workspace_id, ready_machine_id, for_update=True)
        assert enrollment is not None
        enrollments.save(
            enrollment.model_copy(
                update={
                    "readiness_phase": MachineReadinessPhase.Offline,
                    "schedulable": False,
                    "updated_at": datetime.now(UTC),
                }
            )
        )

    failed_inventory = WorkspaceComputeInstanceListResponse.model_validate_json(
        client.get("/api/v1/compute/instances").content
    )
    failed = next(item for item in failed_inventory.data if item.machine_id == ready_machine_id)
    assert failed.service_state is MachineServiceState.Failed
    assert failed.bootstrap_failure_reason is MachineBootstrapFailureReason.WorkerReadinessFailed

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

    with isolated_services.context.database.session() as session:
        workspaces = WorkspaceRepository(session)
        workspace = workspaces.get(workspace_id)
        assert workspace is not None
        workspaces.upsert(workspace.model_copy(update={"status": WorkspaceStatus.Deleting}))
        instances = ComputeProviderInstanceRepository(session)
        draining = instances.list_for_pool(pool_id)[0]
        instances.upsert(draining.model_copy(update={"status": "terminating"}))

    draining_response = client.get("/api/v1/compute/instances")
    assert draining_response.status_code == 200
    draining_inventory = WorkspaceComputeInstanceListResponse.model_validate_json(
        draining_response.content
    )
    assert [(item.id, item.status) for item in draining_inventory.data] == [
        (draining.instance_id, "deleting")
    ]
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
    with isolated_services.context.database.session() as session:
        MachineRepository(session).upsert(
            Machine(
                id=str(uuid4()),
                name="rack-7",
                workspace_ids=(elsewhere.id,),
                pool=MachinePool("rack-7"),
                provider="agent",
                status=ResourceStatus.Running,
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

    assert (pinned.pool, pinned.machine) == ("rack-7", "rack-7")
    assert (located.pool, located.machine) == (LAZYCLOUD_MACHINE_POOL, "")
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
                pool=MachinePool("aws"),
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
