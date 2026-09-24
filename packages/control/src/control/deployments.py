from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from database.repositories.apps import (
    AppRepository,
    CronJobRepository,
    DeploymentRepository,
    StubRepository,
)
from database.repositories.custom_domains import CustomDomainRepository
from database.repositories.identity import WorkspaceMemberRepository
from observability.workspace_changes import WorkspaceChangePublisher
from pydantic import JsonValue
from shared.cron import CronJobRecord, next_cron_run, normalize_cron_expression
from shared.deployment_records import (
    Deployment,
    DeploymentSpec,
    declared_min_containers,
    keeps_one_active_version,
    resolve_authorized,
    resolve_cpu,
    resolve_keep_warm_seconds,
    resolve_max_pending_tasks,
    resolve_memory,
    resolve_pod_command,
    resolve_pod_disks,
    resolve_pod_role,
    resolve_pod_ssh,
    resolve_preemptible,
    resolve_retries,
    resolve_timeout_seconds,
)
from shared.deployment_subdomains import deployment_subdomain
from shared.deployments import DeploymentKind
from shared.errors import ConflictError, InvalidInputError, NotFoundError
from shared.http.workspace_changes import WorkspaceChangeTopic, WorkspaceChangeType
from shared.tasks import RetryPolicy
from shared.timestamps import utc_now
from sqlalchemy.orm import Session

from control.context import ControlContext
from control.deployment_cleanup import (
    DeploymentPlacementResourceManager,
    delete_deployment_cron_jobs,
)
from control.events import ControlEventEmitter
from control.placement import PlacementResolver
from control.tcp_ingress import require_tcp_ingress


@dataclass(frozen=True, slots=True)
class DeploymentRegistration:
    app_id: str
    stub_id: str


@dataclass(frozen=True, slots=True)
class DeploymentAppResolution:
    app_id: str | None
    app_name: str
    """Name the deployment's app has or will be created under.

    Known even when `app_id` is not, which is what lets the subdomain be minted on the
    same deploy that creates the app.
    """


class DeploymentRegistrar(Protocol):
    def resolve_deployment_app(
        self,
        spec: DeploymentSpec,
        *,
        workspace: str = "default",
    ) -> DeploymentAppResolution: ...

    def register_deployment(
        self,
        deployment: Deployment,
        *,
        workspace: str = "default",
    ) -> DeploymentRegistration: ...


class DeploymentScheduleWriter(Protocol):
    """The one thing deploying needs from schedules: make this one's match."""

    def set_for_deployment(
        self,
        deployment: Deployment,
        *,
        cron: str | None,
        workspace: str,
    ) -> CronJobRecord | None: ...


class CustomDomainUseAdmission(Protocol):
    def assert_may_use_custom_domains(
        self,
        session: Session,
        *,
        user_id: str,
    ) -> None: ...


def deployment_machine(spec: DeploymentSpec) -> str:
    """The joined machine a spec pins to, from its metadata, or empty."""
    machine = spec.metadata.get("machine")
    return machine.strip() if isinstance(machine, str) else ""


@dataclass(frozen=True, slots=True)
class DeploymentService:
    context: ControlContext
    events: ControlEventEmitter
    placement: PlacementResolver
    registrar: DeploymentRegistrar
    schedules: DeploymentScheduleWriter
    custom_domain_admission: CustomDomainUseAdmission
    workspace_changes: WorkspaceChangePublisher | None = None
    placement_resources: DeploymentPlacementResourceManager | None = None

    def deploy(self, spec: DeploymentSpec, *, workspace: str = "default") -> Deployment:
        normalized_spec = _normalize_runtime_spec(spec)
        machine = deployment_machine(normalized_spec)
        with self.context.database.session() as session:
            workspace_record = self.context.workspace(session, workspace)
            resolved_placement = self.placement.resolve_placement(
                session, workspace_record, machine
            )
        app_resolution = self.registrar.resolve_deployment_app(
            normalized_spec,
            workspace=workspace_record.id,
        )
        app_id = app_resolution.app_id
        with self.context.database.session() as session:
            repository = DeploymentRepository(session)
            app = (
                AppRepository(session).get_for_update(app_id, workspace_id=workspace_record.id)
                if app_id is not None
                else None
            )
            deployment_active = app.active if app is not None else True
            version = repository.next_version(
                workspace_id=workspace_record.id,
                app_id=app_id,
                name=normalized_spec.name,
                kind=normalized_spec.kind,
            )
            if normalized_spec.kind is DeploymentKind.Function:
                _release_superseded_warm_floors(
                    session,
                    repository.live_stub_ids(
                        workspace_id=workspace_record.id,
                        app_id=app_id,
                        name=normalized_spec.name,
                        kind=normalized_spec.kind,
                    ),
                    workspace_id=workspace_record.id,
                )
            subdomain = deployment_subdomain(
                workspace_id=workspace_record.id,
                app_name=app_resolution.app_name,
                name=normalized_spec.name,
                kind=normalized_spec.kind,
            )
            repository.assert_subdomain_unclaimed(
                subdomain,
                workspace_id=workspace_record.id,
                app_id=app_id,
                name=normalized_spec.name,
                kind=normalized_spec.kind,
            )
            custom_hostname = _claimed_hostname(
                session,
                normalized_spec.domain,
                workspace_id=workspace_record.id,
                admission=self.custom_domain_admission,
            )
            deployment = repository.upsert(
                Deployment(
                    id=str(uuid4()),
                    name=normalized_spec.name,
                    kind=normalized_spec.kind,
                    app_id=app_id,
                    version=version,
                    spec=normalized_spec,
                    subdomain=subdomain,
                    custom_hostname=custom_hostname,
                    placement=resolved_placement,
                    machine=machine,
                    active=deployment_active,
                ),
                workspace_id=workspace_record.id,
            )
        try:
            if self.placement_resources is not None and deployment.active:
                self.placement_resources.reconcile_deployments(
                    workspace=workspace_record.id,
                )
            registration = self.registrar.register_deployment(
                deployment,
                workspace=workspace_record.id,
            )
            deployment = deployment.model_copy(
                update={
                    "app_id": registration.app_id or deployment.app_id,
                    "stub_id": registration.stub_id,
                }
            )
            with self.context.database.session() as session:
                deployment = DeploymentRepository(session).upsert(
                    deployment,
                    workspace_id=workspace_record.id,
                )
            # Register the stub before its schedule can fire. Keep schedule writes
            # inside the compensation block so a failed deploy leaves no schedule.
            self.schedules.set_for_deployment(
                deployment,
                cron=normalized_spec.cron,
                workspace=workspace_record.id,
            )
        except Exception as deployment_failure:
            compensation_failures: list[Exception] = []
            try:
                self._discard_failed_deployment(
                    deployment,
                    workspace_id=workspace_record.id,
                )
            except Exception as cleanup_failure:
                compensation_failures.append(cleanup_failure)
            if self.placement_resources is not None:
                try:
                    self.placement_resources.reconcile_deployments(
                        workspace=workspace_record.id,
                        required=False,
                    )
                except Exception as cleanup_failure:
                    compensation_failures.append(cleanup_failure)
            if compensation_failures:
                raise ExceptionGroup(
                    "deployment failed and compensation was incomplete",
                    [deployment_failure, *compensation_failures],
                ) from None
            raise
        if deployment.active and keeps_one_active_version(deployment.kind, deployment.spec.role):
            # Registered first, so a failed deploy leaves the prior version on.
            with self.context.database.session() as session:
                superseded = DeploymentRepository(session).deactivate_other_versions(
                    deployment,
                    workspace_id=workspace_record.id,
                    older_only=True,
                    now=utc_now(),
                )
            for previous in superseded:
                self._publish_change(
                    previous,
                    workspace_id=workspace_record.id,
                    change=WorkspaceChangeType.Updated,
                )
        self.events.emit(
            "deployment.created",
            resource_type="deployment",
            resource_id=deployment.id,
            message=f"deployed {deployment.name}",
            workspace_id=workspace_record.id,
        )
        self._publish_change(
            deployment,
            workspace_id=workspace_record.id,
            change=WorkspaceChangeType.Created,
        )
        return deployment

    def list(
        self,
        *,
        active: bool | None = None,
        workspace: str | None = None,
        app_id: str | None = None,
    ) -> list[Deployment]:
        with self.context.database.session() as session:
            workspace_id = (
                self.context.workspace(session, workspace).id if workspace is not None else None
            )
            repository = DeploymentRepository(session)
            deployments = (
                repository.list(workspace_id=workspace_id, app_id=app_id, active=active)
                if workspace_id is not None
                else repository.list_across_workspaces(app_id=app_id, active=active)
            )
        deployments.sort(key=lambda item: (item.name, item.version))
        return deployments

    def get(self, deployment_id_or_name: str) -> Deployment:
        """Operator/system resolution by id or name across workspaces."""
        with self.context.database.session() as session:
            repository = DeploymentRepository(session)
            record = repository.get_across_workspaces(deployment_id_or_name)
            if record is not None:
                return record
            matches = [
                item
                for item in repository.list_across_workspaces()
                if item.name == deployment_id_or_name
            ]
        if not matches:
            msg = f"deployment not found: {deployment_id_or_name}"
            raise NotFoundError(msg)
        return max(matches, key=lambda item: item.version)

    def delete(self, deployment_id: str, *, workspace: str = "default") -> Deployment:
        """Delete the workload this deployment is a version of, every version at once."""
        now = utc_now()
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            repository = DeploymentRepository(session)
            target = repository.get(deployment_id, workspace_id=workspace_id)
            if target is None:
                msg = f"deployment not found: {deployment_id}"
                raise NotFoundError(msg)
            deleted = repository.delete_versions(
                workspace_id=workspace_id,
                app_id=target.app_id,
                name=target.name,
                kind=target.kind,
                now=now,
            )
            deleted_target = next(
                (deployment for deployment in deleted if deployment.id == target.id), None
            )
            if deleted_target is None:
                # Another delete got there between the read and the update.
                msg = f"deployment not found: {deployment_id}"
                raise NotFoundError(msg)
            delete_deployment_cron_jobs(
                session,
                deployment_ids={deployment.id for deployment in deleted},
            )
        for deployment in deleted:
            self.events.emit(
                "deployment.deleted",
                resource_type="deployment",
                resource_id=deployment.id,
                message=f"deleted deployment {deployment.name} version {deployment.version}",
                workspace_id=workspace_id,
            )
            self._publish_change(
                deployment,
                workspace_id=workspace_id,
                change=WorkspaceChangeType.Deleted,
            )
        if self.placement_resources is not None:
            self.placement_resources.reconcile_deployments(
                workspace=workspace_id,
                required=False,
            )
        return deleted_target

    def _discard_failed_deployment(
        self,
        deployment: Deployment,
        *,
        workspace_id: str,
    ) -> None:
        now = utc_now()
        failed = deployment.model_copy(
            update={"active": False, "deleted_at": now, "updated_at": now}
        )
        with self.context.database.session() as session:
            DeploymentRepository(session).upsert(
                failed,
                workspace_id=workspace_id,
            )

    def _publish_change(
        self,
        deployment: Deployment,
        *,
        workspace_id: str,
        change: WorkspaceChangeType,
    ) -> None:
        if self.workspace_changes is None:
            return
        self.workspace_changes.emit_change(
            workspace_id=workspace_id,
            topic=WorkspaceChangeTopic.Deployments,
            change=change,
            resource_id=deployment.id,
            app_id=deployment.app_id,
            deployment_id=deployment.id,
            stub_id=deployment.stub_id,
        )


def _claimed_hostname(
    session: Session,
    domain: str | None,
    *,
    workspace_id: str,
    admission: CustomDomainUseAdmission,
) -> str | None:
    """Resolve the hostname a spec claims, refusing one the deployer cannot serve.

    Checked against the registrations held by the account that owns this workspace,
    so a spec cannot claim a name under a domain another tenant proved it owns, and a
    domain registered once serves deployments in any workspace that account owns. A
    registration still short of `ready` is accepted: the certificate arrives on the
    provider's schedule, and a deploy that failed until it did would make an ordinary
    redeploy depend on DNS propagation.
    """

    if domain is None:
        return None
    owner = WorkspaceMemberRepository(session).owner(workspace_id)
    if owner is not None:
        admission.assert_may_use_custom_domains(session, user_id=owner.user_id)
    registered = (
        CustomDomainRepository(session).get_by_hostname(domain, user_id=owner.user_id)
        if owner is not None
        else None
    )
    if registered is None:
        raise InvalidInputError(
            f"{domain} is not registered to this account; "
            f"register it before a deployment can serve it"
        )
    return domain


def _normalize_runtime_spec(spec: DeploymentSpec) -> DeploymentSpec:
    default_retries = resolve_retries(spec.kind, None)
    metadata = dict(spec.metadata)
    role = resolve_pod_role(spec.kind, spec.role)
    if role is not None:
        ssh = metadata.get("ssh")
        metadata["ssh"] = resolve_pod_ssh(role, ssh if isinstance(ssh, bool) else None)
    if "authorized" not in metadata:
        metadata["authorized"] = resolve_authorized(spec.kind, None)
    if metadata.get("tcp") is True:
        if spec.kind is not DeploymentKind.Pod or not spec.ports:
            raise InvalidInputError("raw TCP ingress requires a Pod with an exposed port")
        require_tcp_ingress(public=metadata["authorized"] is False)
    if "max_pending_tasks" not in metadata:
        max_pending_tasks = resolve_max_pending_tasks(spec.kind, None)
        if max_pending_tasks is not None:
            metadata["max_pending_tasks"] = max_pending_tasks
    return spec.model_copy(
        update={
            "resources": spec.resources.model_copy(
                update={
                    "cpu": resolve_cpu(spec.kind, spec.resources.cpu),
                    "memory": resolve_memory(spec.kind, spec.resources.memory),
                    "timeout_seconds": resolve_timeout_seconds(
                        spec.kind,
                        spec.resources.timeout_seconds,
                    ),
                    "keep_warm": resolve_keep_warm_seconds(
                        spec.kind,
                        spec.resources.keep_warm,
                        min_containers=declared_min_containers(spec.metadata),
                        scheduled=bool(spec.cron),
                        role=role,
                    ),
                    "preemptible": resolve_preemptible(role, spec.resources.preemptible),
                }
            ),
            "role": role,
            "command": resolve_pod_command(role, spec.command),
            "disks": resolve_pod_disks(
                role,
                name=spec.name,
                disks=spec.disks,
                root_disk_bytes=spec.root_disk_bytes,
            ),
            "root_disk_bytes": None,
            "retry_policy": (
                spec.retry_policy
                if spec.retry_policy is not None
                else RetryPolicy.from_retries(default_retries)
                if default_retries > 0
                else None
            ),
            "metadata": metadata,
        }
    )


def _metadata_str(metadata: Mapping[str, JsonValue], key: str) -> str:
    value = metadata.get(key)
    return value if isinstance(value, str) else ""


def _release_superseded_warm_floors(
    session: Session,
    stub_ids: list[str],
    *,
    workspace_id: str,
) -> None:
    """Release prior versions' warm containers while keeping those versions invocable.

    Leave the idle window infinite. Running containers read it from their startup
    environment and would not see a shorter window written here. A zero floor
    lets the autoscaler stop idle containers without interrupting busy ones.
    """

    repository = StubRepository(session)
    for stub_id in stub_ids:
        stub = repository.get(stub_id, workspace_id=workspace_id)
        if stub is None or stub.config.autoscaler.min_containers == 0:
            continue
        config = stub.config.model_copy(deep=True)
        config.autoscaler.min_containers = 0
        stub.config = config
        repository.upsert(stub)


@dataclass(slots=True)
class CronJobService:
    context: ControlContext
    workspace_changes: WorkspaceChangePublisher | None = None

    def set_for_deployment(
        self,
        deployment: Deployment,
        *,
        cron: str | None,
        workspace: str,
    ) -> CronJobRecord | None:
        """Replace or remove the workload's schedule under a deployment row lock.

        The subdomain identifies the schedule across deployment versions. Check
        that this deployment still exists before either write, so a registration
        delayed past pruning cannot change a replacement workload's schedule.
        """
        try:
            normalized_cron = normalize_cron_expression(cron) if cron else None
            next_run_at = next_cron_run(normalized_cron) if normalized_cron else None
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            repository = CronJobRepository(session)
            if normalized_cron is None:
                if not DeploymentRepository(session).lock_live(
                    deployment.id, workspace_id=workspace_id
                ):
                    raise ConflictError("cannot change the schedule of a deleted deployment")
                record = repository.get(deployment.subdomain, workspace_id=workspace_id)
                repository.delete(deployment.subdomain, workspace_id=workspace_id)
            else:
                record = repository.upsert(
                    CronJobRecord(
                        workspace_id=workspace_id,
                        name=deployment.subdomain,
                        cron=normalized_cron,
                        deployment_id=deployment.id,
                        next_run_at=next_run_at,
                    ),
                    workspace_id=workspace_id,
                )
        if record is not None:
            self.publish_change(
                record,
                WorkspaceChangeType.Created if normalized_cron else WorkspaceChangeType.Deleted,
            )
        return record if normalized_cron else None

    def list(self, *, workspace: str = "default") -> list[CronJobRecord]:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            records = CronJobRepository(session).list(workspace_id=workspace_id)
        records.sort(key=lambda item: item.name)
        return records

    def list_all(self) -> list[CronJobRecord]:
        """Scheduler/operator listing over every workspace's cron jobs."""
        with self.context.database.session() as session:
            records = CronJobRepository(session).list_across_workspaces()
        records.sort(key=lambda item: (item.workspace_id, item.name))
        return records

    def delete(self, name: str, *, workspace: str = "default") -> None:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            repository = CronJobRepository(session)
            record = repository.get(name, workspace_id=workspace_id)
            repository.delete(name, workspace_id=workspace_id)
        if record is not None:
            self.publish_change(record, WorkspaceChangeType.Deleted)

    def set_deployment_enabled(
        self,
        deployment_id: str,
        *,
        enabled: bool,
        workspace: str = "default",
    ) -> list[CronJobRecord]:
        now = utc_now()
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            repository = CronJobRepository(session)
            updated: list[CronJobRecord] = []
            for record in repository.list(workspace_id=workspace_id):
                if record.deployment_id != deployment_id:
                    continue
                record.enabled = enabled
                if enabled:
                    record.next_run_at = next_cron_run(record.cron, now)
                record.updated_at = now
                updated.append(repository.upsert(record, workspace_id=workspace_id))
        for record in updated:
            self.publish_change(record, WorkspaceChangeType.Updated)
        return updated

    def publish_change(
        self,
        record: CronJobRecord,
        change: WorkspaceChangeType,
    ) -> None:
        if self.workspace_changes is None:
            return
        self.workspace_changes.emit_change(
            workspace_id=record.workspace_id,
            topic=WorkspaceChangeTopic.Deployments,
            change=change,
            resource_id=record.name,
            deployment_id=record.deployment_id,
        )
