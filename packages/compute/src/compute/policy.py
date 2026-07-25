from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import uuid4

from database.repositories.apps import DeploymentRepository
from database.repositories.compute import (
    AwsAccountConnectionRepository,
    ComputeMachineEnrollmentRepository,
    ComputePoolRepository,
    ComputeProviderInstanceRecord,
    ComputeProviderInstanceRepository,
    WorkspaceComputePolicyRepository,
)
from database.repositories.identity import WorkspaceRepository
from database.repositories.orchestration import PoolRepository, WorkerRepository
from database.types import DatabaseSession
from foundation.resources import parse_memory_mib
from pydantic import ConfigDict, Field, JsonValue
from shared.aws_connections import AwsAccountConnection
from shared.compute_enrollment import (
    MachineBootstrapFailureReason,
    MachineBootstrapPhase,
    MachineReadinessPhase,
)
from shared.compute_fleet import ResourceStatus
from shared.compute_policy import (
    AwsWorkspaceComputePolicy,
    ComputePlacement,
    ComputePlacementSource,
    ComputePlacementTarget,
    ComputePoolPhase,
    ComputePoolRecord,
    ComputeResourceRequirements,
    WorkspaceComputePolicy,
)
from shared.contracts import ContractModel
from shared.deployment_records import Deployment, DeploymentSpec
from shared.errors import ConflictError, InvalidInputError
from shared.identity import WorkspaceStatus
from shared.timestamps import utc_now

from compute.agent_control import agent_machine_worker_id
from compute.context import ComputeContext
from compute.offers import ReservationStatus


class _DeploymentPoolMetadata(ContractModel):
    model_config = ConfigDict(extra="ignore")

    metadata: dict[str, JsonValue] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ComputeCatalogInstance:
    instance_type: str
    kind: str
    cpu_millicores: int
    memory_mb: int
    gpu: str | None = None
    gpu_count: int = 0


@dataclass(frozen=True, slots=True)
class ComputeCatalogRegion:
    region: str
    instances: tuple[ComputeCatalogInstance, ...]


@dataclass(frozen=True, slots=True)
class ComputeInstanceView:
    record: ComputeProviderInstanceRecord
    region: str
    bootstrap_phase: MachineBootstrapPhase
    bootstrap_failure_reason: MachineBootstrapFailureReason | None
    bootstrap_observed_at: datetime


@dataclass(frozen=True, slots=True)
class ComputeWorkloadView:
    deployment: Deployment
    placement: ComputePlacement
    resources: ComputeResourceRequirements


@dataclass(frozen=True, slots=True)
class ComputeSummary:
    policy: WorkspaceComputePolicy
    connection: AwsAccountConnection | None
    instances: tuple[ComputeInstanceView, ...]
    ready_instance_count: int
    pending_instance_count: int
    degraded_instance_count: int
    workload_count: int
    hourly_cost_micros: int


class AwsDefaultCapacityOwner(Protocol):
    def reconcile_aws_default_capacity(
        self,
        *,
        workspace: str,
        region: str,
        instance_type: str,
        initial_machines: int,
        min_machines: int,
        max_machines: int,
        min_free_cpu_millicores: int,
        min_free_memory_mib: int,
        root_volume_gib: int,
        idle_timeout_seconds: int,
    ) -> ComputePoolRecord: ...

    def clear_aws_default_capacity(self, *, workspace: str, release_capacity: bool) -> None: ...


def _aws_capacity_is_zero(aws: AwsWorkspaceComputePolicy) -> bool:
    return (
        aws.min_cpu_workers == 0
        and aws.initial_cpu_workers == 0
        and aws.max_cpu_instances == 0
        and aws.max_gpu_instances == 0
    )


@dataclass(frozen=True, slots=True)
class AwsDefaultCapacityBaseline:
    capacity: AwsDefaultCapacityOwner

    def reconcile(self, policy: WorkspaceComputePolicy) -> ComputePoolRecord | None:
        if policy.default_placement is not ComputePlacementTarget.Aws:
            # A zero-capacity policy owns zero machines: clearing floors alone
            # would leave durable desired capacity (and billing) behind.
            self.capacity.clear_aws_default_capacity(
                workspace=policy.workspace_id,
                release_capacity=_aws_capacity_is_zero(policy.aws),
            )
            return None
        aws = policy.aws
        return self.capacity.reconcile_aws_default_capacity(
            workspace=policy.workspace_id,
            region=aws.default_region,
            instance_type=aws.default_instance_type,
            initial_machines=aws.initial_cpu_workers,
            min_machines=aws.min_cpu_workers,
            max_machines=aws.max_cpu_instances,
            min_free_cpu_millicores=aws.min_free_cpu_millicores,
            min_free_memory_mib=aws.min_free_memory_mib,
            root_volume_gib=aws.root_volume_gib,
            idle_timeout_seconds=aws.idle_timeout_seconds,
        )


@dataclass(slots=True)
class WorkspaceComputePolicyService:
    context: ComputeContext
    available_catalog: tuple[ComputeCatalogRegion, ...] = ()
    aws_default_capacity: AwsDefaultCapacityBaseline | None = None

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
        default_placement: ComputePlacementTarget,
        aws: AwsWorkspaceComputePolicy,
    ) -> WorkspaceComputePolicy:
        self._validate_aws_policy(aws)
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
            if default_placement is ComputePlacementTarget.Aws:
                connection = AwsAccountConnectionRepository(session).get_for_workspace(workspace_id)
                if connection is None or not connection.accepts_placement:
                    raise ConflictError(
                        "connect and validate AWS before making it the default placement"
                    )
            saved = repository.save(
                current.model_copy(
                    update={
                        "revision": current.revision + 1,
                        "default_placement": default_placement,
                        "aws": aws,
                        "updated_at": utc_now(),
                    }
                )
            )
        if self.aws_default_capacity is not None:
            self.aws_default_capacity.reconcile(saved)
        return saved

    def reconcile_capacity_at_startup(self) -> tuple[ComputePoolRecord, ...]:
        baseline = self.aws_default_capacity
        if baseline is None:
            return ()
        with self.context.database.session() as session:
            active_workspace_ids = {
                workspace.id
                for workspace in WorkspaceRepository(session).list()
                if workspace.status is WorkspaceStatus.Active
            }
            policies = [
                policy
                for policy in WorkspaceComputePolicyRepository(
                    session
                ).records.list_across_workspaces()
                if policy.workspace_id in active_workspace_ids
            ]
        return tuple(
            pool for policy in policies if (pool := baseline.reconcile(policy)) is not None
        )

    def resolve_placement(
        self,
        *,
        workspace: str,
        requested: ComputePlacementTarget | None = None,
        attached_pool: str = "",
        requirements: ComputeResourceRequirements | None = None,
    ) -> ComputePlacement:
        del requirements
        if requested is not None and attached_pool:
            raise InvalidInputError(
                "workload placement cannot combine an explicit target with a self-hosted pool"
            )
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            policy = self._policy_in_session(session, workspace_id)
            if attached_pool:
                pool = ComputePoolRepository(session).get_by_name(workspace_id, attached_pool)
                if pool is not None:
                    return self._attached_pool_placement(pool)
                scheduler_pool = PoolRepository(session).get(
                    attached_pool,
                    workspace_id=workspace_id,
                )
                if scheduler_pool is not None:
                    return ComputePlacement(
                        target=ComputePlacementTarget.Managed,
                        source=ComputePlacementSource.AttachedPool,
                        provider=scheduler_pool.provider,
                        pool_name=scheduler_pool.name,
                    )
                raise InvalidInputError(f"attached compute pool {attached_pool!r} was not found")
            target = requested if requested is not None else policy.default_placement
            source = (
                ComputePlacementSource.WorkloadOverride
                if requested is not None
                else ComputePlacementSource.WorkspaceDefault
            )
            if target is ComputePlacementTarget.Managed:
                return ComputePlacement(
                    target=target,
                    source=source,
                    provider=ComputePlacementTarget.Managed.value,
                    region="",
                )
            connection = AwsAccountConnectionRepository(session).get_for_workspace(workspace_id)
            if connection is None or not connection.accepts_placement:
                raise ConflictError("AWS placement requires a ready workspace connection")
            region = policy.aws.default_region
            if region not in policy.aws.allowed_regions:
                raise InvalidInputError(f"AWS region {region!r} is not allowed by workspace policy")
            return ComputePlacement(
                target=target,
                source=source,
                provider=ComputePlacementTarget.Aws.value,
                region=region,
                provider_ref=f"aws:{connection.id}",
            )

    def resolve_deployment_placement(
        self,
        spec: DeploymentSpec,
        *,
        workspace: str,
    ) -> ComputePlacement:
        return self.resolve_placement(
            workspace=workspace,
            requested=spec.placement,
            attached_pool=_deployment_pool_name(spec),
        )

    def catalog(
        self,
        *,
        workspace: str,
    ) -> tuple[tuple[str, tuple[ComputeCatalogInstance, ...]], ...]:
        self.get_policy(workspace=workspace)
        return tuple((item.region, item.instances) for item in self.available_catalog)

    def instances(self, *, workspace: str) -> tuple[ComputeInstanceView, ...]:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            pools = ComputePoolRepository(session).list_internal(workspace_id=workspace_id)
            instances = ComputeProviderInstanceRepository(session)
            enrollments = ComputeMachineEnrollmentRepository(session)
            workers = WorkerRepository(session)
            views = [
                _compute_instance_view(
                    record,
                    region=pool.region,
                    workspace_id=workspace_id,
                    pool_name=pool.name,
                    enrollments=enrollments,
                    workers=workers,
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
                        placement=deployment.resolved_placement,
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
            connection = AwsAccountConnectionRepository(session).get_for_workspace(workspace_id)
        ready_instance_count = sum(
            item.bootstrap_phase is MachineBootstrapPhase.Ready for item in instances
        )
        pending_instance_count = sum(
            item.bootstrap_phase
            in {
                MachineBootstrapPhase.Requested,
                MachineBootstrapPhase.Provisioning,
                MachineBootstrapPhase.Joining,
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
            hourly_cost_micros=sum(item.record.hourly_cost_micros for item in instances),
        )

    def assert_workspace_deletable(self, *, workspace: str) -> None:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            connection = AwsAccountConnectionRepository(session).get_for_workspace(workspace_id)
            pools = ComputePoolRepository(session).list_internal(workspace_id=workspace_id)
        if connection is not None or any(
            pool.phase is not ComputePoolPhase.Deleted for pool in pools
        ):
            raise ConflictError("disconnect AWS compute before deleting this workspace")

    def _resolve_in_session(
        self,
        *,
        session: DatabaseSession,
        workspace_id: str,
        requested: ComputePlacementTarget | None,
        attached_pool: str,
    ) -> ComputePlacement:
        if requested is not None and attached_pool:
            raise InvalidInputError(
                "workload placement cannot combine an explicit target with a self-hosted pool"
            )
        policy = self._policy_in_session(session, workspace_id)
        if attached_pool:
            pool = ComputePoolRepository(session).get_by_name(workspace_id, attached_pool)
            if pool is not None:
                return self._attached_pool_placement(pool)
            scheduler_pool = PoolRepository(session).get(
                attached_pool,
                workspace_id=workspace_id,
            )
            if scheduler_pool is not None:
                return ComputePlacement(
                    target=ComputePlacementTarget.Managed,
                    source=ComputePlacementSource.AttachedPool,
                    provider=scheduler_pool.provider,
                    pool_name=scheduler_pool.name,
                )
            raise InvalidInputError(f"attached compute pool {attached_pool!r} was not found")
        target = requested if requested is not None else policy.default_placement
        source = (
            ComputePlacementSource.WorkloadOverride
            if requested is not None
            else ComputePlacementSource.WorkspaceDefault
        )
        if target is ComputePlacementTarget.Managed:
            return ComputePlacement(target=target, source=source, provider="managed")
        connection = AwsAccountConnectionRepository(session).get_for_workspace(workspace_id)
        if connection is None or not connection.accepts_placement:
            raise ConflictError("AWS placement requires a ready workspace connection")
        region = policy.aws.default_region
        if region not in policy.aws.allowed_regions:
            raise InvalidInputError(f"AWS region {region!r} is not allowed by workspace policy")
        return ComputePlacement(
            target=target,
            source=source,
            provider="aws",
            region=region,
            provider_ref=f"aws:{connection.id}",
        )

    @staticmethod
    def _attached_pool_placement(pool: ComputePoolRecord) -> ComputePlacement:
        aws = pool.provider_ref.startswith("aws:")
        return ComputePlacement(
            target=ComputePlacementTarget.Aws if aws else ComputePlacementTarget.Managed,
            source=ComputePlacementSource.AttachedPool,
            provider="aws" if aws else "managed",
            region=pool.region if aws else "",
            pool_name=pool.name,
            provider_ref=pool.provider_ref,
        )

    @staticmethod
    def _requirements(deployment: Deployment) -> ComputeResourceRequirements:
        return WorkspaceComputePolicyService._requirements_for_spec(deployment.spec)

    @staticmethod
    def _requirements_for_spec(spec: DeploymentSpec) -> ComputeResourceRequirements:
        resources = spec.resources
        return ComputeResourceRequirements(
            cpu_millicores=int((resources.cpu or 0) * 1000),
            memory_mb=_memory_mb(resources.memory),
            gpu=resources.gpu,
            gpu_count=resources.gpu_count,
        )

    def _validate_aws_policy(self, policy: AwsWorkspaceComputePolicy) -> None:
        instances_by_region = {
            region.region: {instance.instance_type for instance in region.instances}
            for region in self.available_catalog
        }
        unavailable_regions = set(policy.allowed_regions) - instances_by_region.keys()
        if unavailable_regions:
            raise InvalidInputError(
                "AWS regions are not available for configured capacity: "
                + ", ".join(sorted(unavailable_regions))
            )
        available_types = {
            instance_type
            for region in policy.allowed_regions
            for instance_type in instances_by_region[region]
        }
        unavailable_types = set(policy.allowed_instance_types) - available_types
        if unavailable_types:
            raise InvalidInputError(
                "AWS instance types are not available in allowed regions: "
                + ", ".join(sorted(unavailable_types))
            )
        default_region_types = instances_by_region[policy.default_region]
        if policy.default_instance_type not in default_region_types:
            raise InvalidInputError(
                "AWS default instance type is not available in the default region: "
                f"{policy.default_instance_type}"
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


def _memory_mb(value: str | None) -> int:
    return parse_memory_mib(value) or 0


def _compute_instance_view(
    record: ComputeProviderInstanceRecord,
    *,
    region: str,
    workspace_id: str,
    pool_name: str,
    enrollments: ComputeMachineEnrollmentRepository,
    workers: WorkerRepository,
) -> ComputeInstanceView:
    phase = record.bootstrap_phase
    failure_reason = record.bootstrap_failure_reason
    observed_at = record.bootstrap_observed_at
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
            pool_name=pool_name,
        )
        worker = workers.get(
            agent_machine_worker_id(record.machine_id),
            workspace_id=workspace_id,
        )
        if (
            enrollment is not None
            and enrollment.readiness_phase is MachineReadinessPhase.Ready
            and worker is not None
            and worker.status is ResourceStatus.Running
        ):
            phase = MachineBootstrapPhase.Ready
            failure_reason = None
            observed_at = max(observed_at, enrollment.updated_at, worker.last_seen_at)
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
        bootstrap_failure_reason=failure_reason,
        bootstrap_observed_at=observed_at,
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
    "ComputeCatalogInstance",
    "ComputeCatalogRegion",
    "ComputeInstanceView",
    "ComputeSummary",
    "ComputeWorkloadView",
    "WorkspaceComputePolicyService",
]
