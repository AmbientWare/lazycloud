from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import uuid4

from database.records.apps import StubRecord
from database.repositories.apps import AppRepository, StubRepository
from database.repositories.deployment_effects import (
    DeploymentAction,
    DeploymentEffect,
    DeploymentEffectRepository,
)
from database.repositories.deployments import DeploymentRepository
from database.repositories.identity import WorkspaceRepository
from database.repositories.orchestration import AutoscalingTargetRepository
from shared.app_lifecycle import UNFINISHED_APP_LIFECYCLE_STATES
from shared.app_slug import app_slug_or_default
from shared.autoscaler_state import autoscaler_target_kind
from shared.deployment_records import (
    Deployment,
    DeploymentSpec,
    keeps_one_active_version,
    resolve_authorized,
)
from shared.deployment_subdomains import deployment_subdomain
from shared.deployments import DeploymentKind, StubKind
from shared.errors import ConflictError, InvalidInputError, NotFoundError
from shared.http.workspace_changes import WorkspaceChangeType
from shared.timestamps import utc_now
from sqlalchemy.orm import Session

from control.apps import AppService
from control.context import ControlContext
from control.cron_jobs import CronJobService
from control.deployment_config import (
    deployment_spec_from_stub,
    deployment_stub_config,
    normalize_deployment_spec,
)
from control.deployment_domains import CustomDomainUseAdmission, claimed_hostname
from control.deployment_effects import DeploymentEffects
from control.deployment_resources import DeploymentResource
from control.placement import PlacementResolver
from control.readers import DatabaseDeploymentReader
from control.stubs import StubService

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DeploymentService:
    context: ControlContext
    placement: PlacementResolver
    apps: AppService
    stubs: StubService
    schedules: CronJobService
    custom_domain_admission: CustomDomainUseAdmission
    effects: DeploymentEffects

    def deploy(self, spec: DeploymentSpec, *, workspace: str = "default") -> Deployment:
        return self._publish(spec, workspace=workspace).deployment

    def deploy_prepared(
        self, stub_id: str, *, name: str = "", workspace: str | None = "default"
    ) -> DeploymentResource:
        source = self.stubs.get_stub(stub_id, workspace=workspace)
        return self._publish(
            deployment_spec_from_stub(source, name=name or source.name),
            workspace=source.workspace_id,
            source=source,
        )

    def _publish(
        self, spec: DeploymentSpec, *, workspace: str, source: StubRecord | None = None
    ) -> DeploymentResource:
        spec = normalize_deployment_spec(spec)
        machine = spec.metadata.get("machine")
        machine = machine.strip() if isinstance(machine, str) else ""
        with self.context.database.session() as session:
            owner = self.context.workspace(session, workspace)
            placement = self.placement.resolve_placement(session, owner, machine)
        draft = Deployment(
            id=str(uuid4()),
            name=spec.name,
            kind=spec.kind,
            spec=spec,
            subdomain="",
            placement=placement,
            machine=machine,
        )
        try:
            if self.effects.placement_resources is not None:
                with self.context.database.session() as session:
                    DeploymentEffectRepository(session).prepare(
                        draft, workspace_id=owner.id, now=utc_now()
                    )
                self.effects.placement_resources.reconcile_deployments(workspace=owner.id)
            with self.context.database.session() as session:
                if self.effects.placement_resources is not None and not DeploymentEffectRepository(
                    session
                ).finish_preparation(draft.id, now=utc_now()):
                    raise ConflictError("deployment preparation expired; deploy again")
                WorkspaceRepository(session).lock_active_owner(owner.id)
                current_owner = self.context.workspace(session, owner.id)
                if self.placement.resolve_placement(session, current_owner, machine) != placement:
                    raise ConflictError("workspace placement changed during deployment")
                if source is not None:
                    current_source = StubRepository(session).get_for_update(
                        source.id, workspace_id=owner.id
                    )
                    if current_source != source:
                        raise ConflictError("prepared workload changed; prepare and deploy again")
                source_id = spec.metadata.get("stub_id")
                if source is None and isinstance(source_id, str) and source_id:
                    source = StubRepository(session).get(source_id, workspace_id=owner.id)
                app_name = self._app_name(session, spec, source, workspace_id=owner.id)
                app, app_change, _ = self.apps.create_in_session(
                    session, app_name, workspace=owner.id
                )
                repository = DeploymentRepository(session)
                version = repository.next_version(
                    workspace_id=owner.id, app_id=app.id, name=spec.name, kind=spec.kind
                )
                subdomain = deployment_subdomain(
                    workspace_id=owner.id, app_name=app.name, name=spec.name, kind=spec.kind
                )
                repository.assert_subdomain_unclaimed(
                    subdomain, workspace_id=owner.id, app_id=app.id, name=spec.name, kind=spec.kind
                )
                deployment = repository.upsert(
                    draft.model_copy(
                        update={
                            "app_id": app.id,
                            "version": version,
                            "subdomain": subdomain,
                            "active": app.active,
                            "custom_hostname": claimed_hostname(
                                session,
                                spec.domain,
                                workspace_id=owner.id,
                                admission=self.custom_domain_admission,
                            ),
                        }
                    ),
                    workspace_id=owner.id,
                )
                authorized = spec.metadata.get("authorized")
                stub, _ = self.stubs.save(
                    session,
                    StubRecord(
                        id=str(uuid4()),
                        workspace_id=owner.id,
                        name=spec.name,
                        kind=StubKind(spec.kind.value),
                        handler=spec.handler,
                        app_id=app.id,
                        deployment_id=deployment.id,
                        public=source.public
                        if source
                        else not resolve_authorized(
                            spec.kind, authorized if isinstance(authorized, bool) else None
                        ),
                        config=(
                            source.config.model_copy(deep=True)
                            if source
                            else deployment_stub_config(spec)
                        ).model_copy(update={"machine": machine}),
                        metadata={
                            **(source.metadata if source else {}),
                            "deployment_id": deployment.id,
                        },
                    ),
                    reuse_existing=False,
                )
                deployment = repository.upsert(
                    deployment.model_copy(update={"stub_id": stub.id}), workspace_id=owner.id
                )
                app = AppRepository(session).upsert(
                    app.model_copy(
                        update={
                            "stub_id": stub.id,
                            "version": version,
                            "updated_at": deployment.updated_at,
                            "metadata": {
                                **app.metadata,
                                "deployment_id": deployment.id,
                                "deployment_kind": deployment.kind.value,
                            },
                        }
                    )
                )
                self.schedules.set_in_session(
                    session, deployment, cron=spec.cron, workspace_id=owner.id
                )
                if spec.kind is DeploymentKind.Function:
                    self._release_floors(session, deployment, workspace_id=owner.id)
                source_change = (
                    self.stubs.discard_source_in_session(session, source.id, workspace_id=owner.id)
                    if source is not None
                    else None
                )
                effects = self._supersede(session, deployment, workspace_id=owner.id)
                effects.append(
                    DeploymentEffectRepository(session).record(
                        deployment,
                        workspace_id=owner.id,
                        action=DeploymentAction.Created,
                        app_created=app_change is WorkspaceChangeType.Created,
                        source_stub_id=source_change[0].id if source_change else None,
                        source_deleted=source_change is not None
                        and source_change[1] is WorkspaceChangeType.Deleted,
                    )
                )
        except Exception:
            # The provider owner retains reconciliation intent if this cleanup fails.
            if self.effects.placement_resources is not None:
                try:
                    with self.context.database.session() as session:
                        DeploymentEffectRepository(session).finish_preparation(
                            draft.id, now=utc_now()
                        )
                    self.effects.placement_resources.reconcile_deployments(
                        workspace=owner.id, required=False
                    )
                except Exception:
                    LOGGER.exception("deployment placement cleanup remains pending")
            raise
        self.effects.finish(effects)
        return DeploymentResource(app=app, deployment=deployment, stub=stub)

    def _app_name(
        self,
        session: Session,
        spec: DeploymentSpec,
        source: StubRecord | None,
        *,
        workspace_id: str,
    ) -> str:
        app_id = (
            source.app_id if source is not None and source.app_id else spec.metadata.get("app_id")
        )
        if isinstance(app_id, str) and app_id:
            return self.apps.get_in_session(session, app_id, workspace=workspace_id).name
        name = spec.metadata.get("app")
        return app_slug_or_default(name if isinstance(name, str) else None, default=spec.name)

    def _release_floors(
        self, session: Session, deployment: Deployment, *, workspace_id: str
    ) -> None:
        repository = StubRepository(session)
        for stub in repository.list_for_deployments(
            DeploymentRepository(session).older_active_ids(deployment, workspace_id=workspace_id),
            workspace_id=workspace_id,
            for_update=True,
        ):
            if stub.config.autoscaler.min_containers:
                stub.config.autoscaler.min_containers = 0
                repository.upsert(stub)

    def _supersede(
        self, session: Session, deployment: Deployment, *, workspace_id: str
    ) -> list[DeploymentEffect]:
        if not deployment.active or not keeps_one_active_version(
            deployment.kind, deployment.spec.role
        ):
            return []
        previous = DeploymentRepository(session).deactivate_superseded_versions(
            workspace_id=workspace_id,
            app_id=deployment.app_id,
            name=deployment.name,
            kind=deployment.kind,
            now=utc_now(),
        )
        return [
            self._record_change(
                session, item, workspace_id=workspace_id, action=DeploymentAction.Stopped
            )
            for item in previous
        ]

    def _record_change(
        self,
        session: Session,
        deployment: Deployment,
        *,
        workspace_id: str,
        action: DeploymentAction,
    ) -> DeploymentEffect:
        self.schedules.apply_deployments_in_session(
            session, [deployment], workspace_id=workspace_id
        )
        return DeploymentEffectRepository(session).record(
            deployment, workspace_id=workspace_id, action=action
        )

    def _locked(
        self, session: Session, identifier: str, *, workspace: str
    ) -> tuple[str, Deployment]:
        workspace_id = self.context.workspace(session, workspace).id
        WorkspaceRepository(session).lock_active_owner(workspace_id)
        repository = DeploymentRepository(session)
        deployment = repository.resolve(identifier, workspace_id=workspace_id)
        if deployment is None:
            raise NotFoundError(f"deployment not found in workspace: {identifier}")
        if deployment.app_id is not None:
            app = AppRepository(session).get_for_update(
                deployment.app_id, workspace_id=workspace_id
            )
            if app is None or app.lifecycle_state in UNFINISHED_APP_LIFECYCLE_STATES:
                raise ConflictError("app lifecycle operation is in progress")
        current = repository.get_for_update(deployment.id, workspace_id=workspace_id)
        if current is None:
            raise NotFoundError(f"deployment not found: {identifier}")
        return workspace_id, current

    def set_deployment_active(
        self,
        workspace: str,
        deployment_id_or_name: str,
        *,
        active: bool,
        allow_paused_app: bool = False,
    ) -> Deployment:
        with self.context.database.session() as session:
            workspace_id, deployment = self._locked(
                session, deployment_id_or_name, workspace=workspace
            )
            repository = DeploymentRepository(session)
            if active and deployment.app_id and not allow_paused_app:
                app = AppRepository(session).get(deployment.app_id, workspace_id=workspace_id)
                if app is None or not app.active:
                    raise ConflictError("cannot start deployment while app is paused")
            if active and keeps_one_active_version(deployment.kind, deployment.spec.role):
                newest = repository.newest_active_version(
                    workspace_id=workspace_id,
                    app_id=deployment.app_id,
                    name=deployment.name,
                    kind=deployment.kind,
                )
                if newest is not None and newest > deployment.version:
                    raise ConflictError(
                        f"{deployment.name} v{deployment.version} is replaced by v{newest}; "
                        f"stop v{newest} before starting an older version"
                    )
            deployment = repository.upsert(
                deployment.model_copy(update={"active": active, "updated_at": utc_now()}),
                workspace_id=workspace_id,
            )
            target_kind = autoscaler_target_kind(StubKind(deployment.kind.value))
            if active and deployment.stub_id and target_kind is not None:
                AutoscalingTargetRepository(session).activate(
                    stub_id=deployment.stub_id, workspace_id=workspace_id, target_kind=target_kind
                )
            effects = self._supersede(session, deployment, workspace_id=workspace_id)
            effects.append(
                self._record_change(
                    session,
                    deployment,
                    workspace_id=workspace_id,
                    action=DeploymentAction.Started if active else DeploymentAction.Stopped,
                )
            )
        self.effects.finish(effects)
        return deployment

    def delete(self, identifier: str, *, workspace: str = "default") -> Deployment:
        with self.context.database.session() as session:
            workspace_id, target = self._locked(session, identifier, workspace=workspace)
            deleted = DeploymentRepository(session).delete_versions(
                workspace_id=workspace_id,
                app_id=target.app_id,
                name=target.name,
                kind=target.kind,
                now=utc_now(),
            )
            effects = [
                self._record_change(
                    session, item, workspace_id=workspace_id, action=DeploymentAction.Deleted
                )
                for item in deleted
            ]
        self.effects.finish(effects)
        return next(item for item in deleted if item.id == target.id)

    def retrieve_deployment(self, workspace: str, identifier: str) -> Deployment:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            deployment = DeploymentRepository(session).resolve(
                identifier, workspace_id=workspace_id
            )
        if deployment is None:
            raise NotFoundError(f"deployment not found in workspace: {identifier}")
        return deployment

    def stop_all_active_deployments(self, workspace: str) -> tuple[Deployment, ...]:
        return tuple(
            self.set_deployment_active(workspace, deployment.id, active=False)
            for deployment in self.list(workspace=workspace, active=True)
        )

    def scale_deployment(self, workspace: str, identifier: str, *, containers: int) -> Deployment:
        if containers < 0:
            raise InvalidInputError("replicas cannot be negative")
        with self.context.database.session() as session:
            workspace_id, deployment = self._locked(session, identifier, workspace=workspace)
            if deployment.kind is not DeploymentKind.Pod:
                raise InvalidInputError("only pod deployments can be scaled directly")
            app = (
                AppRepository(session).get(deployment.app_id, workspace_id=workspace_id)
                if deployment.app_id
                else None
            )
            if not deployment.active:
                raise ConflictError(f"cannot scale inactive deployment: {deployment.name}")
            if app is not None and not app.active:
                raise ConflictError(f"cannot scale deployment while app is paused: {app.name}")
            stubs = StubRepository(session).list_for_deployments(
                [deployment.id], workspace_id=workspace_id, for_update=True
            )
            if not stubs:
                raise ConflictError("pod deployment has no scalable workload")
            for stub in stubs:
                if containers == 0 and stub.config.runtime.keep_warm == -1:
                    raise InvalidInputError("always-on pod deployments cannot be scaled to zero")
                if containers > 1 and stub.config.disks:
                    raise InvalidInputError(
                        "a pod with a disk runs one container; scale it to 0 or 1"
                    )
                if containers > 0:
                    self.effects.containers.validate_pod_activation(stub.config.runtime)
                stub.config.autoscaler = stub.config.autoscaler.model_copy(
                    update={"min_containers": containers, "max_containers": containers}
                )
                stub.updated_at = utc_now()
                self.stubs.save(session, stub, reuse_existing=False)
            deployment = DeploymentRepository(session).upsert(
                deployment.model_copy(update={"updated_at": utc_now()}), workspace_id=workspace_id
            )
            effect = DeploymentEffectRepository(session).record(
                deployment,
                workspace_id=workspace_id,
                action=DeploymentAction.Scaled,
                message=f"scaled deployment {deployment.name} to {containers} containers",
                data={"containers": containers, "stub_ids": [stub.id for stub in stubs]},
            )
        self.effects.finish([effect])
        return deployment

    def list(
        self, *, active: bool | None = None, workspace: str | None = None, app_id: str | None = None
    ) -> list[Deployment]:
        with self.context.database.session() as session:
            repository = DeploymentRepository(session)
            if workspace is None:
                return repository.list_across_workspaces(app_id=app_id, active=active)
            return repository.list(
                workspace_id=self.context.workspace(session, workspace).id,
                app_id=app_id,
                active=active,
            )

    def get(self, deployment_id_or_name: str) -> Deployment:
        return DatabaseDeploymentReader(self.context).get(deployment_id_or_name)
