from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

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
    resolve_authorized,
    resolve_cpu,
    resolve_keep_warm_seconds,
    resolve_max_pending_tasks,
    resolve_memory,
    resolve_retries,
    resolve_timeout_seconds,
)
from shared.deployment_subdomains import deployment_subdomain
from shared.deployments import DeploymentKind
from shared.errors import InvalidInputError, NotFoundError
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


class DeploymentPoolResolver(Protocol):
    def resolve_deployment_pool(self, spec: DeploymentSpec, *, workspace: str) -> str: ...


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


@dataclass(frozen=True, slots=True)
class DeploymentService:
    context: ControlContext
    events: ControlEventEmitter
    pool_resolver: DeploymentPoolResolver
    registrar: DeploymentRegistrar
    schedules: DeploymentScheduleWriter
    custom_domain_admission: CustomDomainUseAdmission
    workspace_changes: WorkspaceChangePublisher | None = None
    placement_resources: DeploymentPlacementResourceManager | None = None

    def deploy(self, spec: DeploymentSpec, *, workspace: str = "default") -> Deployment:
        normalized_spec = _normalize_runtime_spec(spec)
        with self.context.database.session() as session:
            workspace_record = self.context.workspace(session, workspace)
        resolved_pool = self.pool_resolver.resolve_deployment_pool(
            normalized_spec,
            workspace=workspace_record.id,
        )
        app_resolution = self.registrar.resolve_deployment_app(
            normalized_spec,
            workspace=workspace_record.id,
        )
        app_id = app_resolution.app_id
        with self.context.database.session() as session:
            repository = DeploymentRepository(session)
            app = (
                AppRepository(session).get(app_id, workspace_id=workspace_record.id)
                if app_id is not None
                else None
            )
            deployment_active = app.active if app is not None else True
            existing = [
                deployment
                for deployment in repository.list(
                    workspace_id=workspace_record.id,
                    include_deleted=True,
                )
                if deployment.name == normalized_spec.name
                and deployment.kind is normalized_spec.kind
                and deployment.app_id == app_id
            ]
            version = max((item.version for item in existing), default=0) + 1
            if normalized_spec.kind is DeploymentKind.Function:
                _release_superseded_warm_floors(
                    session,
                    existing,
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
            deployment = repository.records.create(
                {
                    "name": normalized_spec.name,
                    "kind": normalized_spec.kind,
                    "app_id": app_id,
                    "stub_id": None,
                    "version": version,
                    "spec": normalized_spec.model_dump(mode="json"),
                    "subdomain": subdomain,
                    "custom_hostname": custom_hostname,
                    "pool": resolved_pool,
                    "active": deployment_active,
                },
                workspace_id=workspace_record.id,
                name=normalized_spec.name,
                status="active" if deployment_active else "inactive",
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
                deployment = DeploymentRepository(session).records.upsert(
                    deployment,
                    workspace_id=workspace_record.id,
                    name=deployment.name,
                    status="active" if deployment.active else "inactive",
                )
            # Unconditional, because a spec without a schedule is stating that
            # this resource has none — and the row a prior version wrote is
            # named for the same subdomain. After the stub exists, because a
            # schedule names the stub it fires, and inside this block so a
            # failure here is compensated with the rest of the deploy rather
            # than leaving a schedule for a deployment that was rolled back.
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

    def delete(self, deployment_id_or_name: str) -> Deployment:
        deployment = self.get(deployment_id_or_name)
        now = utc_now()
        deployment.active = False
        deployment.deleted_at = now
        deployment.updated_at = now
        with self.context.database.session() as session:
            repository = DeploymentRepository(session)
            workspace_id = repository.workspace_id(deployment.id)
            if workspace_id is None:
                msg = f"deployment workspace not found: {deployment.id}"
                raise NotFoundError(msg)
            updated = repository.records.upsert(
                deployment,
                workspace_id=workspace_id,
                name=deployment.name,
                status="inactive",
            )
            delete_deployment_cron_jobs(
                session,
                deployment_ids={deployment.id},
            )
        self.events.emit(
            "deployment.deleted",
            resource_type="deployment",
            resource_id=deployment.id,
            message=f"deleted deployment {deployment.name}",
            workspace_id=workspace_id,
        )
        self._publish_change(
            updated,
            workspace_id=workspace_id,
            change=WorkspaceChangeType.Deleted,
        )
        if self.placement_resources is not None:
            self.placement_resources.reconcile_deployments(
                workspace=workspace_id,
                required=False,
            )
        return updated

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
            DeploymentRepository(session).records.upsert(
                failed,
                workspace_id=workspace_id,
                name=failed.name,
                status="inactive",
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
    covering = (
        CustomDomainRepository(session).covering(domain, user_id=owner.user_id)
        if owner is not None
        else None
    )
    if covering is None:
        raise InvalidInputError(
            f"no domain registered to this account covers {domain}; "
            f"register it before a deployment can serve it"
        )
    return domain


def _normalize_runtime_spec(spec: DeploymentSpec) -> DeploymentSpec:
    default_retries = resolve_retries(spec.kind, None)
    metadata = dict(spec.metadata)
    if "authorized" not in metadata:
        metadata["authorized"] = resolve_authorized(spec.kind, None)
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
                    ),
                }
            ),
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
    existing: list[Deployment],
    *,
    workspace_id: str,
) -> None:
    """Let prior versions of this function drain.

    A warm floor is asked for on a resource, not on a version of it: an author
    who wants two interpreters resident wants two, not two per deploy. It is
    held per stub and every version keeps its own, so without this each deploy
    pins another floor's worth of containers that no name resolves to — and a
    floor makes the idle window infinite, so nothing else ever removes them.

    The version stays active and invocable by number, which is the contract
    prior versions have. It stops being warm, which costs it a cold start and
    nothing else.

    The window is deliberately left infinite while the floor goes to zero. A
    container reads its keep-warm seconds from the environment it was started
    with, so restoring a finite window here would reach the config and not the
    containers already holding the old floor: they would retire on a window
    nothing told them about, which is to say never. Zero floor with no window is
    what puts them under the autoscaler, which stops the idle ones on the next
    tick and leaves the busy ones alone.
    """

    repository = StubRepository(session)
    for prior in existing:
        if not prior.active or prior.deleted_at is not None or not prior.stub_id:
            continue
        stub = repository.get(prior.stub_id, workspace_id=workspace_id)
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
        """Make this deployment's schedule match what its spec declared.

        One call for both answers, because the absence of a schedule is a fact a
        deploy states as deliberately as its presence. Called only where a spec
        declares one, the row a previous version wrote would outlive the source
        line that asked for it: the author deletes `cron=`, redeploys, and the
        old version keeps firing on a schedule nothing in their code names.

        Takes the deployment rather than an id because the caller has already
        resolved it, and because looking it up here would mean depending on the
        service that deploys — which depends on the registration that calls this.

        Named for the deployment's subdomain, which is the one identity a
        resource keeps across its versions and is already checked for collisions
        when it is minted. So a redeploy addresses the previous version's row
        without anything having to go looking for it.
        """

        if not cron:
            self.delete(deployment.subdomain, workspace=workspace)
            return None
        try:
            normalized_cron = normalize_cron_expression(cron)
            next_run_at = next_cron_run(normalized_cron)
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            repository = CronJobRepository(session)
            record = CronJobRecord(
                workspace_id=workspace_id,
                name=deployment.subdomain,
                cron=normalized_cron,
                deployment_id=deployment.id,
                next_run_at=next_run_at,
            )
            saved = repository.upsert(record, workspace_id=workspace_id)
        self.publish_change(saved, WorkspaceChangeType.Created)
        return saved

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
            record = next(
                (item for item in repository.list(workspace_id=workspace_id) if item.name == name),
                None,
            )
            repository.records.delete(name, workspace_id=workspace_id)
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
