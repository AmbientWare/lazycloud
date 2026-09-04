from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta

import pytest
from api.server.services import ApiServices
from compute.aws_connections import (
    AwsAccountConnectionService,
    AwsAccountConnectionValidationError,
    AwsAccountPoolDrain,
    AwsAuthorizationCleanupResult,
)
from compute.bucket_access import AwsConnectionBucketAccessReconciler
from compute.catalog import ComputeCatalogInstance, ComputeCatalogRegion
from compute.policy import WorkspaceComputePolicyService
from database.repositories.compute import (
    AwsAccountConnectionRepository,
    AwsAuthorizationCleanupTombstoneRepository,
)
from database.types import DatabaseSession
from shared.aws_connections import (
    AwsAccountAuthorizationGeneration,
    AwsAccountAuthorizationMode,
    AwsAccountAuthorizationPlan,
    AwsAccountConnection,
    AwsAccountConnectionErrorCode,
    AwsAccountConnectionPhase,
    AwsAccountNetwork,
    AwsAccountValidationResult,
    AwsAuthorizationCleanupStatus,
    AwsManagedAuthorizationReference,
)
from shared.compute_policy import MachinePool
from shared.errors import ConflictError, UpstreamUnavailableError
from shared.http.aws_connections import (
    AwsConnectionCreateRequest,
    AwsConnectionReconnectRequest,
    AwsFleetEnsureRequest,
)
from shared.timestamps import utc_now
from tests.service_fixtures import owned_workspace, workspace_owner_user_id

ACCOUNT_ID = "123456789012"
TEMPLATE_SHA256 = "a" * 64


@dataclass(frozen=True, slots=True)
class _ConnectedCloudAdmission:
    def assert_may_use_connected_cloud(
        self,
        session: DatabaseSession,
        *,
        user_id: str,
    ) -> None:
        del session, user_id


@dataclass(slots=True)
class _Planner:
    def plan(
        self,
        *,
        user_id: str,
        connection_id: str,
        generation: int,
        account_id: str,
        external_id: str,
        role_arn: str | None,
        active_authorization: AwsAccountAuthorizationGeneration | None,
        node_role_arn: str | None,
        node_instance_profile_arn: str | None,
        network: AwsAccountNetwork | None = None,
    ) -> AwsAccountAuthorizationPlan:
        del user_id, connection_id, external_id, active_authorization
        managed = role_arn is None
        return AwsAccountAuthorizationPlan(
            role_arn=role_arn or f"arn:aws:iam::{account_id}:role/control-g{generation}",
            authorization_mode=(
                AwsAccountAuthorizationMode.ManagedStack
                if managed
                else AwsAccountAuthorizationMode.ExistingRole
            ),
            managed_authorization=(
                AwsManagedAuthorizationReference(
                    stack_name=f"connection-g{generation}",
                    region="us-east-1",
                    generation=generation,
                    template_version="test.v1",
                    template_sha256=TEMPLATE_SHA256,
                )
                if managed
                else None
            ),
            authorization_url=(
                f"https://console.aws.amazon.com/cloudformation/g{generation}" if managed else None
            ),
            network=network,
            node_role_arn=node_role_arn or f"arn:aws:iam::{account_id}:role/node",
            node_instance_profile_arn=node_instance_profile_arn
            or f"arn:aws:iam::{account_id}:instance-profile/node",
        )


@dataclass(slots=True)
class _Validator:
    failures: list[AwsAccountConnectionErrorCode] = field(default_factory=list)

    def validate(
        self,
        connection: AwsAccountConnection,
        authorization: AwsAccountAuthorizationGeneration,
    ) -> AwsAccountValidationResult:
        if self.failures:
            code = self.failures.pop(0)
            raise AwsAccountConnectionValidationError("not authorized yet", code=code)
        assert connection.node_role_arn is not None
        assert connection.node_instance_profile_arn is not None
        managed = authorization.managed_authorization
        return AwsAccountValidationResult(
            account_id=connection.account_id,
            role_arn=authorization.role_arn,
            managed_authorization=(
                managed.model_copy(
                    update={
                        "stack_id": (
                            f"arn:aws:cloudformation:us-east-1:{connection.account_id}:"
                            f"stack/{managed.stack_name}/id"
                        )
                    }
                )
                if managed is not None
                else None
            ),
            node_role_arn=connection.node_role_arn,
            node_instance_profile_arn=connection.node_instance_profile_arn,
            validated_at=utc_now(),
        )


@dataclass(slots=True)
class _Lifecycle:
    results: list[AwsAuthorizationCleanupStatus] = field(
        default_factory=lambda: [AwsAuthorizationCleanupStatus.Complete]
    )
    operation_ids: list[str] = field(default_factory=list)
    remove_node_identity: list[bool] = field(default_factory=list)

    def reconcile_authorization_cleanup(
        self,
        *,
        account_id: str,
        external_id: str,
        authorization: AwsAccountAuthorizationGeneration,
        operation_id: str,
        node_role_arn: str | None,
        node_instance_profile_arn: str | None,
        remove_node_identity: bool,
    ) -> AwsAuthorizationCleanupResult:
        del account_id, external_id, authorization, node_role_arn, node_instance_profile_arn
        self.operation_ids.append(operation_id)
        self.remove_node_identity.append(remove_node_identity)
        status = self.results.pop(0) if self.results else AwsAuthorizationCleanupStatus.Complete
        return AwsAuthorizationCleanupResult(status=status)


@dataclass(slots=True)
class _Drainer:
    remaining: int = 0

    def request_connection_drain(
        self,
        connection_id: str,
        *,
        workspace_ids: Sequence[str],
    ) -> AwsAccountPoolDrain:
        del connection_id, workspace_ids
        return AwsAccountPoolDrain(total_pools=1, remaining_pools=self.remaining)


@dataclass(slots=True)
class _BucketAccessReconciler:
    failures: int = 0
    calls: int = 0

    def reconcile_connection(self, connection: AwsAccountConnection) -> None:
        assert connection.bucket_access_reconcile_pending
        self.calls += 1
        if self.failures:
            self.failures -= 1
            raise UpstreamUnavailableError("temporary IAM failure")


@dataclass(slots=True)
class _CapacityBaseline:
    workspaces: list[str] = field(default_factory=list)

    def reconcile_workspace_baseline(self, workspace_id: str) -> None:
        self.workspaces.append(workspace_id)


def _owner(services: ApiServices, *, workspace: str = "default") -> str:
    """The account that owns a workspace, which is what a connection now belongs to."""
    with services.context.database.session() as session:
        workspace_id = services.context.workspace(session, workspace).id
    return workspace_owner_user_id(services.context, workspace_id)


def _service(
    services: ApiServices,
    *,
    lifecycle: _Lifecycle | None = None,
    validator: _Validator | None = None,
    bucket_access: AwsConnectionBucketAccessReconciler | None = None,
    capacity_baseline: _CapacityBaseline | None = None,
) -> AwsAccountConnectionService:
    return AwsAccountConnectionService(
        context=services.context,
        authorization_planner=_Planner(),
        validator=validator or _Validator(),
        authorization_lifecycle=lifecycle or _Lifecycle(),
        pool_drainer=_Drainer(),
        admission=_ConnectedCloudAdmission(),
        bucket_access_reconciler=bucket_access,
        capacity_baseline=capacity_baseline,
    )


def test_bucket_access_reconciliation_retries_through_durable_connection_claim(
    isolated_services: ApiServices,
) -> None:
    bucket_access = _BucketAccessReconciler(failures=1)
    owner = _owner(isolated_services)
    service = _service(isolated_services, bucket_access=bucket_access)
    service.connect(AwsConnectionCreateRequest(account_id=ACCOUNT_ID), user_id=owner)
    ready = service.validate(user_id=owner)
    with isolated_services.context.database.session() as session:
        repository = AwsAccountConnectionRepository(session)
        current = repository.get(ready.id, for_update=True)
        assert current is not None
        repository.save(
            current.model_copy(
                update={
                    "bucket_access_reconcile_pending": True,
                    "next_reconcile_at": utc_now(),
                }
            )
        )

    failed = service.reconcile_due()

    assert failed.processed_count == 1
    assert failed.completed_count == 0
    pending = service.get(user_id=owner)
    assert pending.bucket_access_reconcile_pending
    assert pending.next_reconcile_at is not None
    with isolated_services.context.database.session() as session:
        repository = AwsAccountConnectionRepository(session)
        current = repository.get(pending.id, for_update=True)
        assert current is not None
        repository.save(current.model_copy(update={"next_reconcile_at": utc_now()}))

    completed = service.reconcile_due()

    assert completed.completed_count == 1
    reconciled = service.get(user_id=owner)
    assert not reconciled.bucket_access_reconcile_pending
    assert reconciled.next_reconcile_at is None
    assert bucket_access.calls == 2


def test_uncompleted_setup_removal_hides_connection_and_reconciles_tombstone(
    isolated_services: ApiServices,
) -> None:
    lifecycle = _Lifecycle()
    owner = _owner(isolated_services)
    service = _service(isolated_services, lifecycle=lifecycle)
    created = service.connect(AwsConnectionCreateRequest(account_id=ACCOUNT_ID), user_id=owner)

    assert created.connection.phase is AwsAccountConnectionPhase.AwaitingAuthorization
    assert created.connection.customer_action_url == created.authorization_url
    assert created.connection.customer_action_label == "Continue in AWS"
    assert service.remove(user_id=owner) is None
    assert service.current(user_id=owner) is None
    with isolated_services.context.database.session() as session:
        assert AwsAuthorizationCleanupTombstoneRepository(session).pending_count() == 1

    batch = service.reconcile_due()

    assert batch.completed_count == 1
    assert lifecycle.remove_node_identity == [True]
    with isolated_services.context.database.session() as session:
        assert AwsAuthorizationCleanupTombstoneRepository(session).pending_count() == 0


def test_cancel_reconnect_preserves_ready_generation_and_placement(
    isolated_services: ApiServices,
) -> None:
    owner = _owner(isolated_services)
    service = _service(isolated_services)
    service.connect(AwsConnectionCreateRequest(account_id=ACCOUNT_ID), user_id=owner)
    ready = service.validate(user_id=owner)
    assert ready.hosts_workloads is True

    reconnecting = service.reconnect(AwsConnectionReconnectRequest(), user_id=owner)
    assert reconnecting.connection.hosts_workloads is True
    assert reconnecting.connection.pending_authorization is not None

    canceled = service.cancel_reconnect(user_id=owner)

    assert canceled.phase is AwsAccountConnectionPhase.Ready
    assert canceled.hosts_workloads is True
    assert canceled.active_authorization is not None
    assert canceled.active_authorization.generation == 1
    assert canceled.pending_authorization is None
    assert canceled.customer_action_url is None
    assert canceled.customer_action_label == ""


def test_initial_assume_role_miss_remains_authorization_required(
    isolated_services: ApiServices,
) -> None:
    owner = _owner(isolated_services)
    service = _service(
        isolated_services,
        validator=_Validator(failures=[AwsAccountConnectionErrorCode.AssumeRoleDenied]),
    )
    created = service.connect(AwsConnectionCreateRequest(account_id=ACCOUNT_ID), user_id=owner)

    failed = service.validate(user_id=owner)

    assert failed.phase is AwsAccountConnectionPhase.AwaitingAuthorization
    assert failed.hosts_workloads is False
    assert failed.pending_authorization is not None
    assert failed.pending_authorization.authorization_url == created.authorization_url
    assert failed.pending_authorization.error_code is AwsAccountConnectionErrorCode.AssumeRoleDenied


def test_active_removal_reuses_provider_operation_across_restart_safe_observation(
    isolated_services: ApiServices,
) -> None:
    lifecycle = _Lifecycle(
        results=[
            AwsAuthorizationCleanupStatus.Pending,
            AwsAuthorizationCleanupStatus.Verifying,
            AwsAuthorizationCleanupStatus.Complete,
        ]
    )
    owner = _owner(isolated_services)
    service = _service(isolated_services, lifecycle=lifecycle)
    service.connect(AwsConnectionCreateRequest(account_id=ACCOUNT_ID), user_id=owner)
    service.validate(user_id=owner)

    removing = service.remove(user_id=owner)
    assert removing is not None
    assert removing.phase is AwsAccountConnectionPhase.DisconnectDraining
    assert removing.hosts_workloads is False
    assert service.reconcile_due().processed_count == 1

    for _ in range(3):
        current = service.get(user_id=owner)
        assert current.next_reconcile_at is not None
        with isolated_services.context.database.session() as session:
            row = AwsAccountConnectionRepository(session).get(current.id, for_update=True)
            assert row is not None
            AwsAccountConnectionRepository(session).save(
                row.model_copy(update={"next_reconcile_at": utc_now()})
            )
        service.reconcile_due()

    assert service.current(user_id=owner) is None
    assert len(set(lifecycle.operation_ids)) == 1
    assert lifecycle.remove_node_identity == [True, True, True]


def test_stuck_provider_cleanup_becomes_action_required_after_bounded_attempts(
    isolated_services: ApiServices,
) -> None:
    lifecycle = _Lifecycle(
        results=[
            AwsAuthorizationCleanupStatus.Pending,
            AwsAuthorizationCleanupStatus.Pending,
        ]
    )
    owner = _owner(isolated_services)
    service = _service(isolated_services, lifecycle=lifecycle)
    service.cleanup_max_attempts = 2
    service.connect(AwsConnectionCreateRequest(account_id=ACCOUNT_ID), user_id=owner)
    service.validate(user_id=owner)
    service.remove(user_id=owner)
    service.reconcile_due()

    for _ in range(2):
        current = service.get(user_id=owner)
        with isolated_services.context.database.session() as session:
            row = AwsAccountConnectionRepository(session).get(current.id, for_update=True)
            assert row is not None
            AwsAccountConnectionRepository(session).save(
                row.model_copy(update={"next_reconcile_at": utc_now()})
            )
        service.reconcile_due()

    stuck = service.get(user_id=owner)
    assert stuck.phase is AwsAccountConnectionPhase.ActionRequired
    assert stuck.next_reconcile_at is None
    assert stuck.customer_action_label == "Review AWS cleanup"
    assert "automatic retry window" in stuck.last_error


def test_connection_claim_is_exclusive_and_stale_writer_is_fenced(
    isolated_services: ApiServices,
) -> None:
    owner = _owner(isolated_services)
    service = _service(isolated_services)
    connection = service.connect(
        AwsConnectionCreateRequest(account_id=ACCOUNT_ID), user_id=owner
    ).connection
    now = utc_now()
    lease_until = now + timedelta(seconds=30)
    with isolated_services.context.database.session() as session:
        first = AwsAccountConnectionRepository(session).claim_due(
            now=now,
            lease_until=lease_until,
            limit=1,
        )
    with isolated_services.context.database.session() as session:
        competing = AwsAccountConnectionRepository(session).claim_due(
            now=now,
            lease_until=lease_until,
            limit=1,
        )
    assert len(first) == 1
    assert competing == []

    restarted_at = lease_until + timedelta(seconds=1)
    with isolated_services.context.database.session() as session:
        replacement = AwsAccountConnectionRepository(session).claim_due(
            now=restarted_at,
            lease_until=restarted_at + timedelta(seconds=30),
            limit=1,
        )
    assert len(replacement) == 1
    assert replacement[0].claim_token != first[0].claim_token

    with isolated_services.context.database.session() as session:
        stale = AwsAccountConnectionRepository(session).finish_claim(
            first[0],
            first[0].model_copy(update={"next_reconcile_at": None}),
        )
    assert stale is None
    assert service.get(user_id=owner).id == connection.id


def test_first_connection_reaching_ready_holds_the_accounts_warm_baseline(
    isolated_services: ApiServices,
) -> None:
    """The pass that drives a connection to Ready is the one that must apply it.

    A first connection settles on Ready with no further reconcile scheduled, so
    a baseline applied only by a later pass over an already-Ready connection
    never runs. The account then holds the floor it asked for as a number and no
    machine, until a configuration write that may never come.
    """
    owner = _owner(isolated_services)
    baseline = _CapacityBaseline()
    service = _service(isolated_services, capacity_baseline=baseline)
    service.connect(AwsConnectionCreateRequest(account_id=ACCOUNT_ID), user_id=owner)

    ready = service.validate(user_id=owner)

    assert ready.phase is AwsAccountConnectionPhase.Ready
    assert ready.next_reconcile_at is None
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.workspace(session, "default").id
    assert baseline.workspaces == [workspace_id]


def test_a_workspace_without_its_own_account_still_reaches_the_shared_fleet(
    isolated_services: ApiServices,
) -> None:
    """What the shared fleet is for, and what naming a pool decides.

    The second workspace here is a customer who connected nothing, which is the
    ordinary case on a fleet the platform runs. Resolving only their own
    connection left them able to use capacity that happened to exist and unable
    to cause any, so their workload deployed, queued, and died on a retry limit
    reporting that it needed a GPU worker, naming neither the fleet nor the
    account. The pool answers instead: theirs when they connected one, and
    whatever feeds it otherwise.
    """
    owner = _owner(isolated_services)
    service = _service(isolated_services)
    service.connect(
        AwsConnectionCreateRequest(account_id=ACCOUNT_ID),
        user_id=owner,
        platform_fleet=True,
    )
    fleet = service.validate(user_id=owner)
    assert fleet.hosts_workloads is True
    assert fleet.platform_fleet is True

    # A separate account, holding no connection of its own.
    customer = owned_workspace(isolated_services.control_plane_service, "customer")
    policies = WorkspaceComputePolicyService(isolated_services.context)

    resolved = policies.connection_for_machine_pool(workspace=customer.name, pool=fleet.pool)

    assert resolved is not None, "a customer on the shared fleet reaches the fleet's account"
    assert resolved.account_id == ACCOUNT_ID
    # A pool nothing feeds still answers None, so naming one cannot conjure an
    # account to buy machines in.
    assert (
        policies.connection_for_machine_pool(workspace=customer.name, pool=MachinePool("nobody"))
        is None
    )


def test_fleet_ensure_preserves_authorization_and_reconciles_limits(
    isolated_services: ApiServices,
) -> None:
    service = _service(isolated_services)
    service.available_catalog = (
        ComputeCatalogRegion(
            region="us-east-1",
            instances=(
                ComputeCatalogInstance(
                    instance_type="m7i.large", kind="cpu", cpu_millicores=2000, memory_mb=8192
                ),
            ),
        ),
    )
    owner = _owner(isolated_services)
    request = AwsFleetEnsureRequest(
        account_id=ACCOUNT_ID,
        role_arn=f"arn:aws:iam::{ACCOUNT_ID}:role/fleet",
        external_id="fleet-ensure-test-external-identifier",
        network=AwsAccountNetwork(
            vpc_id="vpc-01234567",
            subnet_ids=("subnet-01234567", "subnet-89abcdef"),
            security_group_id="sg-01234567",
        ),
        max_cpu_instances=500,
        max_gpu_instances=100,
    )
    created = service.ensure_fleet(request, user_id=owner)
    assert service.ensure_fleet(request, user_id=owner) == created
    configured = service.ensure_fleet(
        request.model_copy(update={"max_cpu_instances": 200, "max_gpu_instances": 0}),
        user_id=owner,
    )
    assert configured.compute.max_cpu_instances == 200
    assert configured.compute.max_gpu_instances == 0
    assert configured.compute.revision == created.compute.revision + 1
    assert configured.pending_authorization == created.pending_authorization
    assert configured.external_id == created.external_id


def test_fleet_ensure_rejects_changed_infrastructure_without_changing_policy(
    isolated_services: ApiServices,
) -> None:
    service = _service(isolated_services)
    owner = _owner(isolated_services)
    request = AwsFleetEnsureRequest(
        account_id=ACCOUNT_ID,
        role_arn=f"arn:aws:iam::{ACCOUNT_ID}:role/fleet",
        external_id="fleet-ensure-test-external-identifier",
        network=AwsAccountNetwork(
            vpc_id="vpc-01234567",
            subnet_ids=("subnet-01234567", "subnet-89abcdef"),
            security_group_id="sg-01234567",
        ),
        max_cpu_instances=500,
        max_gpu_instances=100,
    )
    created = service.ensure_fleet(request, user_id=owner)
    changed = request.model_copy(
        update={
            "network": request.network.model_copy(update={"vpc_id": "vpc-ffffffff"}),
            "max_cpu_instances": 200,
        }
    )
    with pytest.raises(ConflictError, match="infrastructure"):
        service.ensure_fleet(changed, user_id=owner)
    assert service.get(user_id=owner) == created
