from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import uuid4

from database.repositories.apps import DeploymentRepository
from database.repositories.compute import (
    AwsAccountConnectionRepository,
    ComputeMachineEnrollmentRepository,
    ComputeProviderInstanceRecord,
    ComputeProviderInstanceRepository,
    ComputeUnitRepository,
    WorkspaceComputePolicyRepository,
)
from database.repositories.identity import WorkspaceMemberRepository, WorkspaceRepository
from database.types import DatabaseSession
from pydantic import ConfigDict, Field, JsonValue
from shared.aws_connections import AwsAccountConnection
from shared.compute_enrollment import (
    MachineBootstrapFailureReason,
    MachineBootstrapPhase,
    MachineReadinessPhase,
    MachineServiceState,
)
from shared.compute_policy import (
    ComputeResourceRequirements,
    ComputeUnitPhase,
    ComputeUnitRecord,
    MachinePool,
    WorkspaceComputePolicy,
)
from shared.contracts import ContractModel
from shared.deployment_records import Deployment, DeploymentSpec, request_and_limit
from shared.errors import ConflictError
from shared.identity import WorkspaceStatus
from shared.resources import parse_memory_mib
from shared.timestamps import utc_now

from compute.agent_control import (
    MachineWorkerState,
    machine_serves_workloads,
)
from compute.aws_configuration import AWS_COMPUTE_CONFIGURATION, AwsComputeConfiguration
from compute.catalog import ComputeCatalogInstance, ComputeCatalogRegion
from compute.context import ComputeContext
from compute.offers import ReservationStatus
from compute.provider_machines import _provider_booted_template_version
from database import AsyncDatabaseClient

LOGGER = logging.getLogger(__name__)


class _DeploymentPoolMetadata(ContractModel):
    model_config = ConfigDict(extra="ignore")

    metadata: dict[str, JsonValue] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ComputeInstanceView:
    record: ComputeProviderInstanceRecord
    region: str
    bootstrap_phase: MachineBootstrapPhase
    service_state: MachineServiceState
    bootstrap_failure_reason: MachineBootstrapFailureReason | None
    bootstrap_failure_detail: str
    bootstrap_observed_at: datetime
    booted_template_version: str


@dataclass(frozen=True, slots=True)
class ComputeWorkloadView:
    deployment: Deployment
    pool: MachinePool
    resources: ComputeResourceRequirements


@dataclass(frozen=True, slots=True)
class MachinePoolView:
    """One pool a workload may name, described by the units feeding it."""

    name: str
    is_default: bool
    providers: tuple[str, ...]
    unit_count: int
    gpu_types: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ComputeSummary:
    policy: WorkspaceComputePolicy
    connection: AwsAccountConnection | None
    instances: tuple[ComputeInstanceView, ...]
    ready_instance_count: int
    pending_instance_count: int
    degraded_instance_count: int
    workload_count: int
    hourly_cost_micros: int | None


class AwsDefaultCapacityOwner(Protocol):
    def workspace_has_ready_customer_connection(self, workspace: str) -> bool: ...

    def reconcile_aws_default_capacity(
        self,
        *,
        workspace: str,
        region: str,
        instance_type: str,
        initial_machines: int,
        min_machines: int,
        min_free_cpu_millicores: int,
        min_free_memory_mib: int,
        root_volume_gib: int,
        idle_timeout_seconds: int,
    ) -> ComputeUnitRecord: ...

    def clear_aws_default_capacity(self, *, workspace: str, release_capacity: bool) -> None: ...


def _aws_capacity_is_zero(configuration: AwsComputeConfiguration) -> bool:
    """Whether managed policy disables AWS CPU capacity."""
    return configuration.min_cpu_workers == 0 and configuration.initial_cpu_workers == 0


@dataclass(frozen=True, slots=True)
class AwsDefaultCapacityBaseline:
    capacity: AwsDefaultCapacityOwner

    def reconcile(
        self,
        *,
        workspace_id: str,
        configuration: AwsComputeConfiguration,
    ) -> ComputeUnitRecord | None:
        """Apply the managed AWS warm baseline to a connected workspace."""
        if not self.capacity.workspace_has_ready_customer_connection(workspace_id):
            LOGGER.info(
                "warm baseline for workspace %s declined: no connection hosting workloads",
                workspace_id,
            )
            return None
        if _aws_capacity_is_zero(configuration):
            self.capacity.clear_aws_default_capacity(
                workspace=workspace_id,
                release_capacity=True,
            )
            return None
        return self.capacity.reconcile_aws_default_capacity(
            workspace=workspace_id,
            region=configuration.default_region,
            instance_type=configuration.default_instance_type,
            initial_machines=configuration.initial_cpu_workers,
            min_machines=configuration.min_cpu_workers,
            min_free_cpu_millicores=configuration.min_free_cpu_millicores,
            min_free_memory_mib=configuration.min_free_memory_mib,
            root_volume_gib=configuration.root_volume_gib,
            idle_timeout_seconds=configuration.idle_timeout_seconds,
        )


@dataclass(slots=True)
class WorkspaceComputePolicyService:
    context: ComputeContext
    available_catalog: tuple[ComputeCatalogRegion, ...] = ()
    aws_default_capacity: AwsDefaultCapacityBaseline | None = None
    worker_state: MachineWorkerState | None = None

    def get_policy(self, *, workspace: str) -> WorkspaceComputePolicy:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            repository = WorkspaceComputePolicyRepository(session)
            current = repository.get_for_workspace(workspace_id)
            if current is not None:
                return current
            now = utc_now()
            return repository.ensure_default(
                WorkspaceComputePolicy(
                    id=str(uuid4()),
                    workspace_id=workspace_id,
                    created_at=now,
                    updated_at=now,
                )
            )

    def update_policy(
        self,
        *,
        workspace: str,
        expected_revision: int,
        default_pool: str,
    ) -> WorkspaceComputePolicy:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            repository = WorkspaceComputePolicyRepository(session)
            current = repository.get_for_workspace(workspace_id, for_update=True)
            if current is None:
                now = utc_now()
                current = repository.ensure_default(
                    WorkspaceComputePolicy(
                        id=str(uuid4()),
                        workspace_id=workspace_id,
                        created_at=now,
                        updated_at=now,
                    )
                )
                current = repository.get_for_workspace(workspace_id, for_update=True)
                if current is None:
                    raise RuntimeError("workspace compute policy is unavailable")
            if current.revision != expected_revision:
                raise ConflictError("workspace compute policy revision was superseded")
            return repository.save(
                current.model_copy(
                    update={
                        "revision": current.revision + 1,
                        "default_pool": default_pool or current.default_pool,
                        "updated_at": utc_now(),
                    }
                )
            )

    def reconcile_workspace_baseline(self, workspace_id: str) -> None:
        """Apply the connected account's warm baseline in one workspace it backs.

        Both refusals below leave a workspace with whatever capacity it already
        had and no account of why, which is indistinguishable from a baseline
        that ran and decided nothing. They say which one it was.
        """
        if self.aws_default_capacity is None:
            LOGGER.info(
                "warm baseline for workspace %s skipped: no default capacity owner",
                workspace_id,
            )
            return
        with self.context.database.session() as session:
            connection = AwsAccountConnectionRepository(session).get_for_workspace_owner(
                workspace_id
            )
        if connection is None:
            LOGGER.info(
                "warm baseline for workspace %s skipped: its owner has no connection",
                workspace_id,
            )
            return
        LOGGER.info(
            "warm baseline for workspace %s: %d initial, %d minimum",
            workspace_id,
            AWS_COMPUTE_CONFIGURATION.initial_cpu_workers,
            AWS_COMPUTE_CONFIGURATION.min_cpu_workers,
        )
        self.aws_default_capacity.reconcile(
            workspace_id=workspace_id,
            configuration=AWS_COMPUTE_CONFIGURATION,
        )

    async def reconcile_capacity_at_startup(
        self,
        database: AsyncDatabaseClient,
    ) -> tuple[ComputeUnitRecord, ...]:
        """Restore managed warm capacity for active connected workspaces."""
        baseline = self.aws_default_capacity
        if baseline is None:
            return ()
        targets = await database.run_transaction(self._startup_capacity_targets)
        pools: list[ComputeUnitRecord] = []
        for workspace_id, configuration in targets:
            pool = await asyncio.to_thread(
                baseline.reconcile,
                workspace_id=workspace_id,
                configuration=configuration,
            )
            if pool is not None:
                pools.append(pool)
        return tuple(pools)

    @staticmethod
    def _startup_capacity_targets(
        session: DatabaseSession,
    ) -> list[tuple[str, AwsComputeConfiguration]]:
        active_workspace_ids = {
            workspace.id
            for workspace in WorkspaceRepository(session).list()
            if workspace.status is WorkspaceStatus.Active
        }
        members = WorkspaceMemberRepository(session)
        return [
            (workspace_id, AWS_COMPUTE_CONFIGURATION)
            for connection in AwsAccountConnectionRepository(session).list_all()
            for workspace_id in members.owned_workspace_ids(connection.user_id)
            if workspace_id in active_workspace_ids
        ]

    def default_machine_pool(self, *, workspace: str) -> MachinePool:
        """Pool a workload lands in when it names none."""
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            return self._policy_in_session(session, workspace_id).default_pool

    def connection_for_machine_pool(
        self,
        *,
        workspace: str,
        pool: MachinePool,
    ) -> AwsAccountConnection | None:
        """The ready connection whose units feed this pool, if one does.

        The pool decides, not the caller. A customer who connected their own
        account named a pool with it, so naming that pool provisions in their
        account; naming anything else reaches whatever feeds it. The shared
        fleet is the platform's own connection, so a workspace that connected
        nothing still provisions there, which is what the fleet is for.

        Answering with the caller's own connection alone was the bug this
        replaces. Every account without one then failed the check, so a
        customer on the shared fleet could use capacity that happened to exist
        and could never cause any to be created. It surfaced as a workload that
        deployed, queued, and died on a retry limit reporting that it needed a
        GPU worker, naming neither the fleet nor the account.

        A pool nobody provisions into is still legal — naming a pool creates it
        — so None means the pool is fed by joined machines alone.
        """
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            repository = AwsAccountConnectionRepository(session)
            own = repository.get_for_workspace_owner(workspace_id)
            if own is not None and own.hosts_workloads and own.pool == pool:
                return own
            fleet = [
                candidate
                for candidate in repository.list_all()
                if candidate.platform_fleet and candidate.hosts_workloads and candidate.pool == pool
            ]
        # Sorted rather than first-found: the answer decides where a customer's
        # machines are bought, and a listing order is not a promise.
        return min(fleet, key=lambda candidate: candidate.id, default=None)

    def resolve_deployment_pool(self, spec: DeploymentSpec, *, workspace: str) -> str:
        """Pin the pool a deployment runs in for as long as it exists."""
        named = _deployment_pool_name(spec)
        if named:
            return named
        return self.default_machine_pool(workspace=workspace)

    def pools(self, *, workspace: str) -> tuple[MachinePoolView, ...]:
        """Every pool this workspace can schedule into.

        Derived from the units that feed each pool rather than stored: naming a
        pool creates it, so there is no separate list to keep in step. A pool a
        workload names but nothing feeds yet is absent, which is the useful
        answer — it has no capacity to offer.
        """
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            units = ComputeUnitRepository(session).list_for_workspace(workspace_id)
            default_pool = self._policy_in_session(session, workspace_id).default_pool
        grouped: dict[str, list[ComputeUnitRecord]] = {}
        for unit in units:
            if unit.phase is ComputeUnitPhase.Deleted:
                continue
            grouped.setdefault(unit.pool, []).append(unit)
        return tuple(
            MachinePoolView(
                name=name,
                is_default=name == default_pool,
                providers=tuple(sorted({unit.provider for unit in members})),
                unit_count=len(members),
                gpu_types=tuple(
                    sorted({unit.worker_gpu_type for unit in members if unit.worker_gpu_type})
                ),
            )
            for name, members in sorted(grouped.items())
        )

    def catalog(self) -> tuple[tuple[str, tuple[ComputeCatalogInstance, ...]], ...]:
        """Provider inventory: what may be launched, independent of who is asking."""
        return tuple((item.region, item.instances) for item in self.available_catalog)

    def instances_for_account(
        self,
        *,
        workspace_ids: Sequence[str],
    ) -> tuple[ComputeInstanceView, ...]:
        """Provider capacity running in one account's connected cloud.

        Gathered across every workspace the account owns, because the connection is
        the account's and a customer looking at their own cloud spend should see all
        of it rather than the slice one workspace happens to have provisioned.
        """
        views = [
            view for workspace in workspace_ids for view in self.instances(workspace=workspace)
        ]
        views.sort(key=lambda item: (item.record.status, item.record.id))
        return tuple(views)

    def instances(self, *, workspace: str) -> tuple[ComputeInstanceView, ...]:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            pools = ComputeUnitRepository(session).list_internal(workspace_id=workspace_id)
            instances = ComputeProviderInstanceRepository(session)
            enrollments = ComputeMachineEnrollmentRepository(session)
            if self.worker_state is None:
                msg = "workspace compute policy service requires scheduler worker state"
                raise RuntimeError(msg)
            views = [
                _compute_instance_view(
                    record,
                    region=pool.region,
                    workspace_id=workspace_id,
                    pool=pool.pool,
                    enrollments=enrollments,
                    worker_state=self.worker_state,
                )
                for pool in pools
                for record in instances.list_for_pool(pool.id)
                if record.status
                not in {
                    ReservationStatus.Deleted.value,
                    ReservationStatus.Failed.value,
                }
            ]
        views.sort(key=lambda item: (item.record.status, item.record.id))
        return tuple(views)

    def workloads(self, *, workspace: str) -> tuple[ComputeWorkloadView, ...]:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            deployments = DeploymentRepository(session).list(
                workspace_id=workspace_id,
                active=True,
            )
            views: list[ComputeWorkloadView] = []
            for deployment in deployments:
                requirements = self._requirements(deployment)
                views.append(
                    ComputeWorkloadView(
                        deployment=deployment,
                        pool=deployment.pool,
                        resources=requirements,
                    )
                )
        views.sort(key=lambda item: (item.deployment.name, item.deployment.id))
        return tuple(views)

    def summary(self, *, workspace: str) -> ComputeSummary:
        policy = self.get_policy(workspace=workspace)
        instances = self.instances(workspace=workspace)
        workloads = self.workloads(workspace=workspace)
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            connection = AwsAccountConnectionRepository(session).get_for_workspace_owner(
                workspace_id
            )
        ready_instance_count = sum(
            item.service_state is MachineServiceState.Serving for item in instances
        )
        pending_instance_count = sum(
            item.service_state
            in {
                MachineServiceState.Provisioning,
                MachineServiceState.Joining,
            }
            for item in instances
        )
        return ComputeSummary(
            policy=policy,
            connection=connection,
            instances=instances,
            ready_instance_count=ready_instance_count,
            pending_instance_count=pending_instance_count,
            degraded_instance_count=(
                len(instances) - ready_instance_count - pending_instance_count
            ),
            workload_count=len(workloads),
            hourly_cost_micros=(
                sum(
                    cost
                    for item in instances
                    if (cost := item.record.cost_terms.complete_hourly_cost_micros) is not None
                )
                if all(
                    item.record.cost_terms.complete_hourly_cost_micros is not None
                    for item in instances
                )
                else None
            ),
        )

    @staticmethod
    def _requirements(deployment: Deployment) -> ComputeResourceRequirements:
        return WorkspaceComputePolicyService._requirements_for_spec(deployment.spec)

    @staticmethod
    def _requirements_for_spec(spec: DeploymentSpec) -> ComputeResourceRequirements:
        resources = spec.resources
        # The reservation is what capacity is sized against. A ceiling its author
        # named bounds the container, not the machine chosen for it.
        cpu_request, _ = request_and_limit(resources.cpu)
        memory_request, _ = request_and_limit(resources.memory)
        return ComputeResourceRequirements(
            cpu_millicores=int(float(cpu_request or 0) * 1000),
            memory_mb=_memory_mb(memory_request),
            gpu=list(resources.gpu),
            gpu_count=resources.gpu_count,
        )

    @staticmethod
    def _policy_in_session(
        session: DatabaseSession,
        workspace_id: str,
    ) -> WorkspaceComputePolicy:
        repository = WorkspaceComputePolicyRepository(session)
        policy = repository.get_for_workspace(workspace_id)
        if policy is not None:
            return policy
        now = utc_now()
        return repository.ensure_default(
            WorkspaceComputePolicy(
                id=str(uuid4()),
                workspace_id=workspace_id,
                created_at=now,
                updated_at=now,
            )
        )


def _memory_mb(value: str | int | float | None) -> int:
    return parse_memory_mib(value) or 0


_SERVICE_STATE_BY_PHASE: dict[MachineBootstrapPhase, MachineServiceState] = {
    MachineBootstrapPhase.Requested: MachineServiceState.Provisioning,
    MachineBootstrapPhase.Provisioning: MachineServiceState.Provisioning,
    MachineBootstrapPhase.Booting: MachineServiceState.Joining,
    MachineBootstrapPhase.Joining: MachineServiceState.Joining,
    MachineBootstrapPhase.Failed: MachineServiceState.Failed,
    MachineBootstrapPhase.Deleting: MachineServiceState.Deleting,
}


def _service_state(
    phase: MachineBootstrapPhase,
    *,
    serving: bool,
    served_before: bool = False,
) -> MachineServiceState:
    """What the platform concludes, from what the node reported plus who takes work.

    A machine that served once and does not now is `Degraded`, not `Joining`.
    Reporting it as still joining describes a machine that never worked, which
    is the opposite of what happened and hides the only case where an operator
    has something to look at.
    """
    if serving:
        return MachineServiceState.Serving
    if served_before and phase not in {
        MachineBootstrapPhase.Failed,
        MachineBootstrapPhase.Deleting,
    }:
        return MachineServiceState.Degraded
    return _SERVICE_STATE_BY_PHASE[phase]


def _compute_instance_view(
    record: ComputeProviderInstanceRecord,
    *,
    region: str,
    workspace_id: str,
    pool: MachinePool,
    enrollments: ComputeMachineEnrollmentRepository,
    worker_state: MachineWorkerState,
) -> ComputeInstanceView:
    phase = record.bootstrap_phase
    failure_reason = record.bootstrap_failure_reason
    failure_detail = record.bootstrap_failure_detail
    observed_at = record.bootstrap_observed_at
    serving = False
    if record.status == ReservationStatus.Terminating.value:
        phase = MachineBootstrapPhase.Deleting
    elif record.status == ReservationStatus.Failed.value:
        phase = MachineBootstrapPhase.Failed
        failure_reason = failure_reason or MachineBootstrapFailureReason.Unknown
    elif phase in {MachineBootstrapPhase.Failed, MachineBootstrapPhase.Deleting}:
        if phase is MachineBootstrapPhase.Failed:
            failure_reason = failure_reason or MachineBootstrapFailureReason.Unknown
    elif record.machine_id is not None:
        enrollment = enrollments.by_machine(
            workspace_id,
            record.machine_id,
            pool=pool,
        )
        if machine_serves_workloads(
            enrollment,
            machine_id=record.machine_id,
            worker_state=worker_state,
        ):
            assert enrollment is not None
            serving = True
            failure_reason = None
            observed_at = max(observed_at, enrollment.updated_at)
        elif enrollment is not None and enrollment.readiness_phase in {
            MachineReadinessPhase.Blocked,
            MachineReadinessPhase.Offline,
            MachineReadinessPhase.Revoked,
        }:
            phase = MachineBootstrapPhase.Failed
            failure_reason = failure_reason or MachineBootstrapFailureReason.WorkerReadinessFailed
            observed_at = max(observed_at, enrollment.updated_at)
        elif phase not in {MachineBootstrapPhase.Failed, MachineBootstrapPhase.Deleting}:
            phase = MachineBootstrapPhase.Joining
            if enrollment is not None:
                observed_at = max(observed_at, enrollment.updated_at)
    elif phase not in {MachineBootstrapPhase.Failed, MachineBootstrapPhase.Deleting}:
        phase = MachineBootstrapPhase.Provisioning
    return ComputeInstanceView(
        record=record,
        region=region,
        bootstrap_phase=phase,
        service_state=_service_state(
            phase,
            serving=serving,
            served_before=record.first_served_at is not None,
        ),
        bootstrap_failure_reason=failure_reason,
        bootstrap_failure_detail=failure_detail,
        bootstrap_observed_at=observed_at,
        booted_template_version=_provider_booted_template_version(record),
    )


def _deployment_pool_name(spec: DeploymentSpec) -> str:
    pool = _DeploymentPoolMetadata.model_validate_json(spec.model_dump_json()).metadata.get("pool")
    if isinstance(pool, str):
        return pool.strip()
    if not isinstance(pool, dict):
        return ""
    name = pool.get("name")
    return str(name).strip() if name is not None else ""


__all__ = [
    "AwsDefaultCapacityBaseline",
    "ComputeInstanceView",
    "ComputeSummary",
    "ComputeWorkloadView",
    "MachinePoolView",
    "WorkspaceComputePolicyService",
]
