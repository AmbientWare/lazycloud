from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from uuid import uuid4

from database.records.apps import AutoscalingStubRecord, StubKind, StubRecord
from database.repositories.apps import DeploymentRepository, StubRepository
from database.repositories.cleanup import CleanupRepository
from database.repositories.identity import WorkspaceRepository
from database.repositories.orchestration import AutoscalingTargetRepository, ContainerRepository
from foundation.ids import try_uuid
from observability.workspace_changes import WorkspaceChangePublisher
from pydantic import JsonValue
from shared.autoscaler_state import autoscaler_target_kind
from shared.deployment_records import Deployment
from shared.errors import ConflictError, InvalidInputError, NotFoundError
from shared.http.workspace_changes import WorkspaceChangeType
from shared.timestamps import utc_now
from shared.urls import (
    StubUrlTarget,
    build_container_url,
    build_deployment_url,
    build_pod_url,
    build_stub_url,
)
from shared.workload_config import StubConfig
from sqlalchemy.orm import Session

from control.context import ControlContext
from control.events import publish_workload_change
from control.models import StubConfigUpdateResult, StubUrlPlan
from control.stub_config import (
    StubConfigUpdateValue,
    _assert_disks_keep_their_size,
    _limited_public_config,
    _set_nested_config_value,
    _stub_config_payload,
    _stub_preparation_fingerprint,
)
from control.tcp_ingress import tcp_pod_url


def _stub_ports(stub: StubRecord, *, port: int | None = None) -> list[int]:
    if port is not None:
        return [port]
    return list(stub.config.ports.values()) or list(stub.config.runtime.ports.values())


@dataclass(slots=True)
class StubService:
    context: ControlContext
    workspace_changes: WorkspaceChangePublisher | None = None

    def create_stub(
        self,
        name: str,
        *,
        workspace: str = "default",
        kind: StubKind = StubKind.Function,
        handler: str | None = None,
        deployment_id: str | None = None,
        app_id: str | None = None,
        public: bool = False,
        config: StubConfig | Mapping[str, JsonValue] | None = None,
        metadata: Mapping[str, JsonValue] | None = None,
        reuse_existing: bool = True,
    ) -> StubRecord:
        with self.context.database.session() as session:
            workspace_record = self.context.workspace(session, workspace)
        now = utc_now()
        requested = StubRecord(
            id=str(uuid4()),
            workspace_id=workspace_record.id,
            name=name,
            kind=kind,
            handler=handler,
            deployment_id=deployment_id,
            app_id=app_id,
            public=public,
            config=StubConfig.model_validate(config or {}).model_copy(deep=True),
            metadata=dict(metadata or {}),
            created_at=now,
            updated_at=now,
        )
        with self.context.database.session() as session:
            record, created = self.save(session, requested, reuse_existing=reuse_existing)
        if created:
            publish_workload_change(self.workspace_changes, record, WorkspaceChangeType.Created)
        return record

    def save(
        self, session: Session, requested: StubRecord, *, reuse_existing: bool = True
    ) -> tuple[StubRecord, bool]:
        fingerprint = _stub_preparation_fingerprint(requested) if reuse_existing else None
        WorkspaceRepository(session).lock_active_owner(requested.workspace_id)
        repository = StubRepository(session)
        existing = (
            repository.find_reusable(
                workspace_id=requested.workspace_id, preparation_fingerprint=fingerprint
            )
            if fingerprint is not None
            else None
        )
        if existing is not None and _stub_preparation_fingerprint(existing) != fingerprint:
            repository.set_preparation_fingerprint(
                existing.id, workspace_id=requested.workspace_id, fingerprint=None
            )
            existing = None
        CleanupRepository(session).assert_stub_config_available(
            requested.config, workspace_id=requested.workspace_id, metadata=requested.metadata
        )
        _assert_disks_keep_their_size(
            session, requested.config, workspace_id=requested.workspace_id
        )
        if existing is None:
            record = repository.upsert(requested)
            repository.set_preparation_fingerprint(
                record.id,
                workspace_id=requested.workspace_id,
                fingerprint=fingerprint,
            )
        else:
            record = existing
        target_kind = autoscaler_target_kind(record.kind)
        if target_kind is not None:
            AutoscalingTargetRepository(session).activate(
                stub_id=record.id,
                workspace_id=record.workspace_id,
                target_kind=target_kind,
                due_at=requested.updated_at,
            )
        return record, existing is None

    def stub_app_ids(self, stub_ids: Sequence[str], *, workspace_id: str) -> dict[str, str]:
        """Resolve a page of stubs to their apps in one session."""

        with self.context.database.session() as session:
            return StubRepository(session).app_ids_by_id(stub_ids, workspace_id=workspace_id)

    def get_stub(self, stub_id_or_name: str, *, workspace: str | None = None) -> StubRecord:
        with self.context.database.session() as session:
            return self.get_stub_in_session(session, stub_id_or_name, workspace=workspace)

    def get_stub_in_session(
        self, session: Session, stub_id_or_name: str, *, workspace: str | None = None
    ) -> StubRecord:
        workspace_id = (
            self.context.workspace(session, workspace).id if workspace is not None else None
        )
        repository = StubRepository(session)
        stub_id = try_uuid(stub_id_or_name)
        if stub_id is not None:
            record = (
                repository.get(stub_id, workspace_id=workspace_id)
                if workspace_id is not None
                else repository.get_across_workspaces(stub_id)
            )
            if record is not None:
                return record
        record = StubRepository(session).get_by_name(stub_id_or_name, workspace_id=workspace_id)
        if record is None:
            raise NotFoundError(f"stub not found: {stub_id_or_name}")
        return record

    def get_deployment_stub(
        self, deployment_id: str, *, workspace: str | None
    ) -> StubRecord | None:
        with self.context.database.session() as session:
            workspace_id = (
                self.context.workspace(session, workspace).id if workspace is not None else None
            )
            return StubRepository(session).get_for_deployment(
                deployment_id, workspace_id=workspace_id
            )

    def list_stubs(
        self,
        *,
        workspace: str | None = None,
        app_id: str | None = None,
        deployed_only: bool = False,
    ) -> list[StubRecord]:
        with self.context.database.session() as session:
            workspace_id = (
                self.context.workspace(session, workspace).id if workspace is not None else None
            )
            repository = StubRepository(session)
            records = (
                repository.list(
                    workspace_id=workspace_id, app_id=app_id, deployed_only=deployed_only
                )
                if workspace_id is not None
                else repository.list_across_workspaces(app_id=app_id, deployed_only=deployed_only)
            )
        records.sort(key=lambda item: (item.workspace_id, item.name))
        return records

    def list_autoscaling_stubs(
        self, stub_ids: Sequence[str] | None = None
    ) -> list[AutoscalingStubRecord]:
        with self.context.database.session() as session:
            return StubRepository(session).list_autoscaling_across_workspaces(stub_ids=stub_ids)

    def discard_deployment_registration_stub(
        self, stub_id: str, *, deployment_id: str, workspace: str = "default"
    ) -> None:
        with self.context.database.session() as session:
            workspace_record = self.context.workspace(session, workspace)
            repository = StubRepository(session)
            stub = repository.get_for_update(stub_id, workspace_id=workspace_record.id)
            if stub is None:
                return
            if stub.deployment_id != deployment_id:
                raise ConflictError(f"stub is not owned by deployment registration: {stub.id}")
            if repository.registration_is_bound(stub.id):
                raise ConflictError(f"deployment registration stub is already bound: {stub.id}")
            if not repository.delete(stub.id, workspace_id=workspace_record.id):
                raise ConflictError(
                    f"deployment registration stub could not be discarded: {stub.id}"
                )
        publish_workload_change(self.workspace_changes, stub, WorkspaceChangeType.Deleted)

    def discard_registration_source_stub(self, stub_id: str, *, workspace: str = "default") -> bool:
        """Discard a preparation or release its floor while references remain."""

        with self.context.database.session() as session:
            workspace_record = self.context.workspace(session, workspace)
            repository = StubRepository(session)
            stub = repository.get_for_update(stub_id, workspace_id=workspace_record.id)
            if stub is None:
                return False
            if stub.deployment_id:
                return False
            if repository.registration_is_bound(stub.id):
                if stub.kind is not StubKind.Function or not stub.config.autoscaler.min_containers:
                    return False
                # Registration copied the floor to the deployment. Preserve existing
                # invocations, but let the autoscaler retire idle preparation containers.
                stub.config.autoscaler.min_containers = 0
                stub.updated_at = utc_now()
                repository.upsert(stub)
                repository.set_preparation_fingerprint(
                    stub.id, workspace_id=workspace_record.id, fingerprint=None
                )
                change = WorkspaceChangeType.Updated
            else:
                if not repository.delete(stub.id, workspace_id=workspace_record.id):
                    return False
                change = WorkspaceChangeType.Deleted
        publish_workload_change(self.workspace_changes, stub, change)
        return change is WorkspaceChangeType.Deleted

    def get_stub_config(self, stub_id_or_name: str) -> dict[str, JsonValue]:
        stub = self.get_stub(stub_id_or_name)
        if not stub.public:
            raise PermissionError(f"stub config is not public: {stub_id_or_name}")
        return _limited_public_config(stub.config)

    def update_stub_config(
        self,
        stub_id_or_name: str,
        *,
        workspace: str = "default",
        fields: Mapping[str, StubConfigUpdateValue],
    ) -> StubConfigUpdateResult:
        if not fields:
            raise InvalidInputError("at least one config field is required")
        with self.context.database.session() as session:
            resolved = self.get_stub_in_session(session, stub_id_or_name, workspace=workspace)
            WorkspaceRepository(session).lock_active_owner(resolved.workspace_id)
            stub = StubRepository(session).get_for_update(
                resolved.id, workspace_id=resolved.workspace_id
            )
            if stub is None:
                raise NotFoundError(f"stub not found: {stub_id_or_name}")
            config = _stub_config_payload(stub.config)
            for field_path, value in fields.items():
                _set_nested_config_value(config, field_path, value)
            stub.config = StubConfig.model_validate(config)
            stub.updated_at = utc_now()
            updated_stub, _ = self.save(session, stub, reuse_existing=False)
        publish_workload_change(self.workspace_changes, updated_stub, WorkspaceChangeType.Updated)
        updated = tuple(sorted(fields))
        return StubConfigUpdateResult(
            stub=updated_stub,
            updated_fields=updated,
            message=f"stub config updated successfully: {', '.join(updated)}",
        )

    def stub_url(
        self,
        stub_id_or_name: str,
        *,
        workspace: str | None = None,
        external_url: str = "http://127.0.0.1:9000",
        deployment_id: str | None = None,
        port: int | None = None,
        container_id: str | None = None,
    ) -> StubUrlPlan:
        stub = self.get_stub(stub_id_or_name, workspace=workspace)
        deployment = self._deployment(
            deployment_id or stub.deployment_id or "", workspace_id=stub.workspace_id
        )
        ports = _stub_ports(stub, port=port)
        target = StubUrlTarget(
            kind=stub.kind.value,
            stub_id=stub.id,
            deployment_name=deployment.name if deployment else stub.name,
            deployment_version=deployment.version if deployment else 1,
            subdomain=deployment.subdomain if deployment else "",
            public=stub.public,
            ports=ports,
            route=stub.config.route,
        )
        try:
            if container_id is not None:
                if stub.kind not in {StubKind.Endpoint, StubKind.Asgi}:
                    raise InvalidInputError("container URLs require an Endpoint or ASGI workload")
                with self.context.database.session() as session:
                    container = ContainerRepository(session).get(
                        container_id, workspace_id=stub.workspace_id
                    )
                if container is None or container.stub_id != stub.id:
                    raise NotFoundError("endpoint container not found")
                url = build_container_url(external_url, container.id, path=target.invoke_path)
            elif stub.kind is StubKind.Pod:
                if stub.config.tcp:
                    if deployment is None:
                        raise InvalidInputError("raw TCP ingress requires Pod.deploy()")
                    if not ports:
                        raise InvalidInputError("raw TCP ingress requires an exposed port")
                    url = tcp_pod_url(stub.id, ports[0], public=stub.public)
                else:
                    url = build_pod_url(external_url, target)
            elif stub.kind is StubKind.Sandbox:
                raise InvalidInputError("sandbox URLs require a container-specific exposure")
            elif deployment is not None:
                url = build_deployment_url(external_url, target)
            else:
                url = build_stub_url(external_url, target)
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc
        return StubUrlPlan(
            stub=stub,
            url=url,
            external_url=external_url,
            route_kind=stub.kind,
            deployment=deployment,
        )

    def _deployment(self, deployment_id: str, *, workspace_id: str) -> Deployment | None:
        if not deployment_id:
            return None
        with self.context.database.session() as session:
            repository = DeploymentRepository(session)
            deployment = repository.get(deployment_id, workspace_id=workspace_id)
            if deployment is not None:
                return deployment
            return repository.latest_by_name(deployment_id, workspace_id=workspace_id, active=True)
