from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol

from database.repositories.apps import DeploymentRepository
from database.repositories.aws_connections import AwsAccountConnectionRepository
from database.repositories.compute import (
    ComputeMachineEnrollmentRecord,
    ComputeMachineEnrollmentRepository,
    ComputeProviderInstanceRecord,
    ComputeProviderInstanceRepository,
)
from database.repositories.identity import (
    WorkspaceMemberRepository,
    WorkspaceRecord,
    WorkspaceRepository,
)
from database.repositories.orchestration import MachineRepository
from database.types import DatabaseSession
from shared.aws_connections import AwsAccountConnection
from shared.compute_fleet import PENDING_MACHINE_LIFECYCLES, Machine, MachineLifecycle
from shared.compute_policy import (
    ComputeResourceRequirements,
    ComputeUnitRecord,
)
from shared.deployment_records import Deployment, DeploymentSpec, request_and_limit
from shared.errors import InvalidInputError
from shared.identity import WorkspaceStatus
from shared.placement import Placement
from shared.resources import parse_memory_mib

from compute.aws_configuration import AWS_COMPUTE_CONFIGURATION, AwsComputeConfiguration
from compute.catalog import ComputeCatalogInstance, ComputeCatalogRegion
from compute.context import ComputeContext
from compute.offers import ReservationStatus
from compute.telemetry import enrollment_connected
from database import AsyncDatabaseClient

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ConnectionMachineView:
    """One machine a connected cloud launched, beside the provider row it mirrors."""

    machine: Machine
    instance: ComputeProviderInstanceRecord | None
    enrollment: ComputeMachineEnrollmentRecord | None
    connected: bool


@dataclass(frozen=True, slots=True)
class ComputeWorkloadView:
    deployment: Deployment
    machine: str
    resources: ComputeResourceRequirements


@dataclass(frozen=True, slots=True)
class ComputeSummary:
    connection: AwsAccountConnection | None
    machines: tuple[ConnectionMachineView, ...]
    ready_machine_count: int
    pending_machine_count: int
    degraded_machine_count: int
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

    def resolve_placement(
        self, session: DatabaseSession, workspace: WorkspaceRecord, machine: str
    ) -> Placement:
        """Where a workload of this workspace runs.

        Without a machine it is the workspace's location: the platform fleet, or
        the connected account the workspace was created in. With one it is that
        machine, and only if the machine exists in the owner's account and lists
        this workspace. There is no fallback in either direction; a workload that
        names a machine runs there or not at all.
        """
        members = WorkspaceMemberRepository(session)
        if not machine:
            if workspace.connection_id is None:
                return Placement.platform()
            connection = AwsAccountConnectionRepository(session).get(workspace.connection_id)
            # A workspace may hold several owners; the account that connected the
            # cloud must be one of them, whichever row the database lists first.
            if connection is None or not members.is_owner(
                workspace_id=workspace.id, user_id=connection.user_id
            ):
                raise InvalidInputError(
                    f"workspace {workspace.name!r} is pinned to a connected account "
                    "that no longer belongs to its owner"
                )
            return connection.placement
        record = MachineRepository(session).get_serving_by_name(workspace.id, machine)
        if record is None:
            raise InvalidInputError(
                f"machine {machine!r} is not joined to this account or does not serve "
                f"workspace {workspace.name!r}"
            )
        return Placement.machine(record.id)

    def catalog(self) -> tuple[tuple[str, tuple[ComputeCatalogInstance, ...]], ...]:
        """Provider inventory: what may be launched, independent of who is asking."""
        return tuple((item.region, item.instances) for item in self.available_catalog)

    def connection_machines(self, *, user_id: str) -> tuple[ConnectionMachineView, ...]:
        """Every machine the account's connected cloud has launched and not yet removed.

        Classified by placement: a machine is the connection's because its row
        says `connection:<id>`, whichever workspace of the owner's holds it.
        Draining and terminating nodes stay listed until the provider proves
        them gone, so a customer watching capacity leave sees it go.
        """
        with self.context.database.session() as session:
            connection = AwsAccountConnectionRepository(session).get_for_user(user_id)
            if connection is None:
                return ()
            return self._connection_machines_in_session(session, connection)

    def _connection_machines_in_session(
        self, session: DatabaseSession, connection: AwsAccountConnection
    ) -> tuple[ConnectionMachineView, ...]:
        machines = MachineRepository(session).list_for_placement(connection.placement)
        machine_ids = [machine.id for machine in machines]
        instances = ComputeProviderInstanceRepository(session).list_by_machine_ids(machine_ids)
        enrollments = ComputeMachineEnrollmentRepository(session).list_by_machine_ids(machine_ids)
        views = [
            ConnectionMachineView(
                machine=machine,
                instance=instances.get(machine.id),
                enrollment=(enrollment := enrollments.get(machine.id)),
                connected=enrollment_connected(enrollment),
            )
            for machine in machines
        ]
        views.sort(key=lambda item: (item.machine.lifecycle.value, item.machine.id))
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
                        machine=deployment.machine,
                        resources=requirements,
                    )
                )
        views.sort(key=lambda item: (item.deployment.name, item.deployment.id))
        return tuple(views)

    def summary(self, *, workspace: str) -> ComputeSummary:
        """The connected cloud's capacity as the account sees it, from one of its workspaces.

        Counts and cost come from the same machine list the connection card
        shows, so the two never disagree about what is running.
        """
        workloads = self.workloads(workspace=workspace)
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            connection = AwsAccountConnectionRepository(session).get_for_workspace_owner(
                workspace_id
            )
            machines = (
                self._connection_machines_in_session(session, connection)
                if connection is not None
                else ()
            )
        ready_machine_count = sum(
            item.machine.lifecycle is MachineLifecycle.Ready for item in machines
        )
        pending_machine_count = sum(
            item.machine.lifecycle in PENDING_MACHINE_LIFECYCLES for item in machines
        )
        billed = [
            item.instance
            for item in machines
            if item.instance is not None
            and item.instance.status
            not in {ReservationStatus.Deleted.value, ReservationStatus.Failed.value}
        ]
        return ComputeSummary(
            connection=connection,
            machines=machines,
            ready_machine_count=ready_machine_count,
            pending_machine_count=pending_machine_count,
            degraded_machine_count=len(machines) - ready_machine_count - pending_machine_count,
            workload_count=len(workloads),
            hourly_cost_micros=(
                sum(
                    cost
                    for item in billed
                    if (cost := item.cost_terms.complete_hourly_cost_micros) is not None
                )
                if all(item.cost_terms.complete_hourly_cost_micros is not None for item in billed)
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


def _memory_mb(value: str | int | float | None) -> int:
    return parse_memory_mib(value) or 0


__all__ = [
    "AwsDefaultCapacityBaseline",
    "ComputeSummary",
    "ComputeWorkloadView",
    "ConnectionMachineView",
    "WorkspaceComputePolicyService",
]
