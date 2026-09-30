from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from uuid import uuid4

from database.records.apps import StubRecord
from database.repositories.identity import SecretRepository, WorkspaceRepository
from database.repositories.storage import ObjectRepository, VolumeRepository
from foundation.ids import try_uuid
from observability.workspace_changes import WorkspaceChangePublisher
from pydantic import JsonValue
from shared.errors import NotFoundError, UpstreamUnavailableError
from shared.http.stubs import StubCloneOverrideRequest
from shared.http.workspace_changes import WorkspaceChangeTopic, WorkspaceChangeType
from shared.objects import ObjectRecord
from shared.workload_config import StubConfig
from sqlalchemy.orm import Session
from storage.copying import ObjectCopier
from storage.service import ObjectStorage

from control.apps import AppService
from control.context import ControlContext
from control.events import publish_workload_change
from control.models import StubCloneResult
from control.stub_config import _masked_config_object, _stub_config_payload
from control.stubs import StubService


def _reference_ids(value: JsonValue) -> set[str]:
    if isinstance(value, str):
        ident = try_uuid(value)
        return {ident} if ident is not None else set()
    values = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
    return {ident for item in values for ident in _reference_ids(item)}


def _replace_references(value: JsonValue, remap: Mapping[str, ObjectRecord]) -> JsonValue:
    if isinstance(value, str):
        return remap[value].id if value in remap else value
    if isinstance(value, list):
        return [_replace_references(item, remap) for item in value]
    if isinstance(value, dict):
        return {key: _replace_references(item, remap) for key, item in value.items()}
    return value


@dataclass(slots=True)
class StubCloneService:
    context: ControlContext
    stubs: StubService
    object_storage: ObjectStorage | None = None
    workspace_changes: WorkspaceChangePublisher | None = None

    def clone_stub(
        self,
        stub_id_or_name: str,
        *,
        apps: AppService,
        workspace: str = "default",
        overrides: StubCloneOverrideRequest | None = None,
    ) -> StubCloneResult:
        with self.context.database.session() as session:
            source = self.stubs.get_stub_in_session(session, stub_id_or_name)
            target = self.context.workspace(session, workspace)
            if not source.public and source.workspace_id != target.id:
                raise NotFoundError(f"stub not found: {source.id}")
            config = _stub_config_payload(source.config)
            objects = ObjectRepository(session).for_stub(
                workspace_id=source.workspace_id,
                stub_id=source.id,
                referenced_ids=_reference_ids(config),
            )
        if overrides is not None:
            runtime = config.get("runtime")
            runtime = runtime if isinstance(runtime, dict) else {}
            runtime.update(
                {
                    key: value
                    for key, value in overrides.model_dump(mode="json", exclude_unset=True).items()
                    if value is not None
                }
            )
            config["runtime"] = runtime
        if objects and self.object_storage is None:
            raise UpstreamUnavailableError("stub cloning requires object storage")
        copies = (
            ObjectCopier(self.object_storage).copy(
                objects, source_workspace_id=source.workspace_id, target_workspace_id=target.id
            )
            if self.object_storage is not None
            else nullcontext(dict[str, ObjectRecord]())
        )
        with copies as copied:
            config = {key: _replace_references(value, copied) for key, value in config.items()}
            metadata: dict[str, JsonValue] = {**source.metadata, "source_stub_id": source.id}
            metadata.pop("copied_object_ids", None)
            if copied:
                metadata["copied_object_ids"] = [record.id for record in copied.values()]
            with self.context.database.session() as session:
                WorkspaceRepository(session).lock_active_owner(target.id)
                created_volumes = self._remap_runtime(session, config, workspace_id=target.id)
                cloned, _ = self.stubs.save(
                    session,
                    StubRecord(
                        id=str(uuid4()),
                        workspace_id=target.id,
                        name=source.name,
                        kind=source.kind,
                        handler=source.handler,
                        public=source.public,
                        config=StubConfig.model_validate(config),
                        metadata=metadata,
                    ),
                    reuse_existing=False,
                )
                repository = ObjectRepository(session)
                for record in copied.values():
                    record.metadata["stub_id"] = cloned.id
                    repository.upsert(record, workspace_id=target.id)
                app, change, bound_stub = apps.create_in_session(
                    session,
                    source.name,
                    stub_id=cloned.id,
                    workspace=target.id,
                    public=source.public,
                    metadata={"source_stub_id": source.id},
                )
        publish_workload_change(self.workspace_changes, cloned, WorkspaceChangeType.Created)
        apps.publish_registration(app, change, bound_stub)
        if self.workspace_changes is not None:
            for name in created_volumes:
                self.workspace_changes.emit_change(
                    workspace_id=target.id,
                    topic=WorkspaceChangeTopic.StorageVolumes,
                    change=WorkspaceChangeType.Created,
                    resource_id=name,
                )
        return StubCloneResult(
            source_stub=source,
            cloned_stub=bound_stub or cloned,
            app=app,
            copied_config=_masked_config_object(config),
            copied_objects=tuple(record.id for record in copied.values()),
        )

    def _remap_runtime(
        self, session: Session, config: dict[str, JsonValue], *, workspace_id: str
    ) -> list[str]:
        secrets = config.get("secrets")
        if isinstance(secrets, list):
            names = {
                name
                for item in secrets
                if isinstance(name := item.get("name") if isinstance(item, dict) else item, str)
            }
            available = SecretRepository(session).existing_names(names, workspace_id=workspace_id)
            config["secrets"] = [
                item
                for item in secrets
                if isinstance(name := item.get("name") if isinstance(item, dict) else item, str)
                and name in available
            ]
        volumes = config.get("volumes")
        created: list[str] = []
        if isinstance(volumes, list):
            mounts = [
                (name, item)
                for item in volumes
                if isinstance(item, dict)
                and isinstance(name := item.get("name") or item.get("id"), str)
                and name
            ]
            repository = VolumeRepository(session)
            available_volumes = repository.for_names(
                {name for name, _ in mounts}, workspace_id=workspace_id
            )
            remapped: list[JsonValue] = []
            for name, item in mounts:
                volume = available_volumes.get(name)
                if volume is None:
                    volume, was_created = repository.create(name, workspace_id=workspace_id)
                    available_volumes[name] = volume
                    if was_created:
                        created.append(volume.name)
                mount: dict[str, JsonValue] = {**item, "id": volume.id, "name": volume.name}
                mount.pop("path", None)
                remapped.append(mount)
            config["volumes"] = remapped
        return created
