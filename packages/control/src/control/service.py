from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import uuid4

from database.records.apps import StubKind, StubRecord
from database.repositories.apps import AppRepository, DeploymentRepository, StubRepository
from database.repositories.artifact_cleanup import ArtifactCleanupRepository
from database.repositories.common import (
    GlobalTableRepository,
    TableRepositoryConfig,
    WorkspaceTableRepository,
)
from database.repositories.identity import SecretRepository, new_signing_key
from database.repositories.orchestration import ContainerRepository
from database.repositories.storage import ObjectRepository, VolumeRepository
from database.tables.apps import StubTable
from database.tables.identity import ConcurrencyLimitTable, WorkspaceTable
from foundation.ids import try_uuid
from identity.auth import AuthService
from observability.workspace_changes import WorkspaceChangePublisher
from pydantic import JsonValue, TypeAdapter
from shared.app_identity import DEFAULT_RESOURCE_TYPE
from shared.containers import ContainerRecord, ContainerStatus
from shared.contracts import ContractModel
from shared.deployment_records import Deployment
from shared.env import STUB_ID_ENV
from shared.errors import ConflictError, InvalidInputError, NotFoundError
from shared.http.pods import (
    SandboxCreatedBucket,
    SandboxDashboardStatus,
    SandboxListResponse,
    SandboxRow,
    SandboxStatsResponse,
    SandboxTimeline,
)
from shared.http.workspace_changes import WorkspaceChangeTopic, WorkspaceChangeType
from shared.identity import (
    ConcurrencyLimitRecord,
    TokenKind,
    WorkspaceRecord,
    WorkspaceStatus,
    WorkspaceStorageConfig,
)
from shared.objects import ObjectRecord
from shared.timestamps import utc_now
from shared.urls import (
    InvokeUrlMode,
    StubUrlTarget,
    build_deployment_url,
    build_pod_url,
    build_stub_url,
)
from shared.workload_config import StubConfig
from sqlalchemy.orm import Session

from control.apps import AppReader, AppRegistry
from control.context import ControlContext
from control.models import (
    ConcurrencyAcquireResult,
    ConcurrencyAcquireStatus,
    PublicStubConfigKey,
    StubCloneOverride,
    StubCloneResult,
    StubConfigUpdateResult,
    StubUrlPlan,
    WorkspaceConfigExport,
    WorkspaceCreateResult,
)
from control.sandbox_urls import rewrite_persisted_sandbox_url_visibility


class WorkspaceStorageError(RuntimeError):
    pass


class WorkspaceStorageAlreadyExistsError(ValueError):
    pass


class WorkspaceStorageAuthorizationError(PermissionError):
    pass


class WorkspaceBucketSettings(Protocol):
    bucket: str
    endpoint_url: str | None
    region_name: str
    access_key_id: str
    secret_access_key: str
    session_token: str
    force_path_style: bool


@runtime_checkable
class WorkspaceBucketSettingsProvider(Protocol):
    settings: WorkspaceBucketSettings


class WorkspaceBucketClient(Protocol):
    def create_bucket(self, bucket: str | None = None) -> None: ...

    def validate_bucket_access(self, bucket: str | None = None) -> None: ...


class OwnedWorkspaceBucketClient(WorkspaceBucketClient, Protocol):
    def close(self) -> None: ...


def _workspace_bucket_settings(client: WorkspaceBucketClient) -> WorkspaceBucketSettings:
    if not isinstance(client, WorkspaceBucketSettingsProvider):
        raise WorkspaceStorageError("workspace storage client settings are unavailable")
    return client.settings


_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])
_JSON_VALUE_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
type StubConfigUpdateValue = JsonValue | ContractModel


def _workspace_records(session: Session) -> GlobalTableRepository[WorkspaceRecord]:
    return GlobalTableRepository(
        session,
        TableRepositoryConfig(WorkspaceTable, WorkspaceRecord),
    )


def _stub_records(session: Session) -> WorkspaceTableRepository[StubRecord]:
    return WorkspaceTableRepository(session, TableRepositoryConfig(StubTable, StubRecord))


def _limit_records(session: Session) -> WorkspaceTableRepository[ConcurrencyLimitRecord]:
    return WorkspaceTableRepository(
        session,
        TableRepositoryConfig(ConcurrencyLimitTable, ConcurrencyLimitRecord),
    )


def _stub_config_payload(config: StubConfig) -> dict[str, JsonValue]:
    return _JSON_OBJECT_ADAPTER.validate_json(
        config.model_dump_json(exclude_unset=True, by_alias=True)
    )


def _masked_config(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        masked: dict[str, JsonValue] = {}
        for key, item in value.items():
            lowered = key.lower()
            if any(token in lowered for token in ("secret", "token", "password", "key")):
                masked[key] = "********"
            else:
                masked[key] = _masked_config(item)
        return masked
    if isinstance(value, list):
        return [_masked_config(item) for item in value]
    return value


def _masked_config_object(
    config: StubConfig | Mapping[str, JsonValue],
) -> dict[str, JsonValue]:
    payload = _stub_config_payload(config) if isinstance(config, StubConfig) else dict(config)
    return {key: _masked_config(value) for key, value in payload.items()}


def _limited_public_config(config: StubConfig) -> dict[str, JsonValue]:
    payload = _stub_config_payload(config)
    return {
        key.value: _masked_config(payload[key.value])
        for key in PublicStubConfigKey
        if key.value in payload
    }


def _set_nested_config_value(
    config: dict[str, JsonValue],
    path: str,
    value: StubConfigUpdateValue,
) -> None:
    parts = path.split(".")
    if not parts or any(not part for part in parts):
        msg = "config field path cannot be empty"
        raise InvalidInputError(msg)
    current: dict[str, JsonValue] = config
    for part in parts[:-1]:
        existing = current.get(part)
        if existing is None:
            nested: dict[str, JsonValue] = {}
            current[part] = nested
            current = nested
            continue
        if not isinstance(existing, dict):
            msg = f"config field is not an object: {part}"
            raise InvalidInputError(msg)
        current = existing
    current[parts[-1]] = _stub_config_update_value(value)


def _stub_config_update_value(value: StubConfigUpdateValue) -> JsonValue:
    if isinstance(value, ContractModel):
        return _JSON_VALUE_ADAPTER.validate_json(value.model_dump_json())
    return value


def _local_object_path(path: str) -> Path | None:
    if path.startswith("file://"):
        return Path(path.removeprefix("file://")).expanduser().resolve()
    if "://" in path:
        return None
    return Path(path).expanduser().resolve()


def _replace_config_references(value: JsonValue, remap: dict[str, str]) -> JsonValue:
    if isinstance(value, str):
        return remap.get(value, value)
    if isinstance(value, list):
        return [_replace_config_references(item, remap) for item in value]
    if isinstance(value, dict):
        return {key: _replace_config_references(item, remap) for key, item in value.items()}
    return value


def _replace_config_object_references(
    config: dict[str, JsonValue],
    remap: dict[str, str],
) -> dict[str, JsonValue]:
    return {key: _replace_config_references(value, remap) for key, value in config.items()}


@dataclass(slots=True)
class ControlPlaneService:
    context: ControlContext
    workspace_storage_client: WorkspaceBucketClient | None = None
    workspace_storage_client_factory: (
        Callable[[WorkspaceStorageConfig], OwnedWorkspaceBucketClient] | None
    ) = None
    workspace_changes: WorkspaceChangePublisher | None = None

    def upsert_workspace(
        self,
        name: str = "default",
        *,
        storage: WorkspaceStorageConfig | None = None,
        signing_key_prefix: str | None = None,
        primary_token_id: str | None = None,
        labels: dict[str, str] | None = None,
        metadata: Mapping[str, JsonValue] | None = None,
    ) -> WorkspaceRecord:
        with self.context.database.session() as session:
            repository = _workspace_records(session)
            existing = _workspace_by_name(repository.list(), name)
            now = utc_now()
            metadata_payload = dict(metadata) if metadata is not None else {}
            if existing is None:
                return repository.create(
                    {
                        "name": name,
                        "status": WorkspaceStatus.Active.value,
                        "signing_key_prefix": signing_key_prefix,
                        "signing_key": new_signing_key(signing_key_prefix),
                        "primary_token_id": primary_token_id,
                        "storage": (storage or WorkspaceStorageConfig()).model_dump(mode="json"),
                        "labels": labels or {},
                        "metadata": metadata_payload,
                        "created_at": now,
                        "updated_at": now,
                    },
                    name=name,
                    status=WorkspaceStatus.Active.value,
                )
            if existing.status is not WorkspaceStatus.Active:
                raise ConflictError(f"workspace name is retained after deletion: {name}")
            existing.storage = storage or existing.storage
            existing.signing_key_prefix = signing_key_prefix or existing.signing_key_prefix
            if not existing.signing_key:
                existing.signing_key = new_signing_key(existing.signing_key_prefix)
            existing.primary_token_id = primary_token_id or existing.primary_token_id
            existing.labels.update(labels or {})
            existing.metadata.update(metadata_payload)
            existing.updated_at = now
            return repository.upsert(
                existing,
                name=existing.name,
                status=existing.status.value,
            )

    def create_workspace(
        self,
        name: str | None = None,
        *,
        storage: WorkspaceStorageConfig | None = None,
    ) -> WorkspaceCreateResult:
        workspace_name = name or f"workspace-{uuid4()}"
        workspace = self.upsert_workspace(workspace_name, storage=storage)
        raw_token, token_record = AuthService(self.context).create_token(
            f"{workspace.name}-primary",
            kind=TokenKind.WorkspacePrimary,
            workspace_id=workspace.id,
        )
        workspace = self.upsert_workspace(workspace.name, primary_token_id=token_record.id)
        if storage is None:
            workspace = self.ensure_workspace_storage(workspace.id)
        return WorkspaceCreateResult(
            workspace_id=workspace.id,
            token=raw_token,
            workspace=workspace,
        )

    def workspace_signing_key(self, workspace: str = "default") -> str:
        return self.get_workspace(workspace).signing_key

    def set_workspace_storage(
        self,
        workspace: str,
        storage: WorkspaceStorageConfig,
    ) -> WorkspaceRecord:
        record = self.get_workspace(workspace)
        record.storage = storage
        record.updated_at = utc_now()
        with self.context.database.session() as session:
            return _workspace_records(session).upsert(
                record,
                name=record.name,
                status=record.status.value,
            )

    def provision_bootstrap_workspace_storage(self, workspace: str = "default") -> str:
        """Give the bootstrap workspace its storage at control-plane start.

        It is created through the identity repository, which cannot know about
        object storage, so it is the one workspace that never passes through
        `create_workspace`. Every other workspace either provisions on creation
        or is deliberately waiting to attach a bucket of its own, so a broader
        sweep would hand those a platform bucket they never asked for.
        """
        try:
            record = self.get_workspace(workspace)
        except (NotFoundError, KeyError):
            return ""
        if record.storage.bucket:
            return ""
        self.ensure_workspace_storage(record.id)
        return record.name

    def ensure_workspace_storage(self, workspace: str) -> WorkspaceRecord:
        """Provision workspace storage once, idempotently.

        Every workspace needs it: volumes and outputs have no fallback tier, so a
        workspace without storage cannot run ordinary work.
        """
        record = self.get_workspace(workspace)
        if record.storage.bucket:
            return record
        return self.create_workspace_storage(record.id)

    def create_workspace_storage(
        self,
        workspace: str,
        *,
        bucket_prefix: str = "workspace",
        backend: str = "s3",
        config: dict[str, JsonValue] | None = None,
        actor_workspace_id: str | None = None,
        token_id_for_cache_invalidation: str | None = None,
    ) -> WorkspaceRecord:
        workspace_record = self.get_workspace(workspace)
        self._validate_storage_attach_allowed(
            workspace_record,
            actor_workspace_id=actor_workspace_id,
        )
        client = self._default_workspace_storage_client()
        bucket = f"{bucket_prefix}-{workspace_record.id}".replace("_", "-")
        storage = self._default_workspace_storage(
            client=client,
            bucket=bucket,
            # The bucket already belongs to one workspace, so no prefix is
            # needed. It stays meaningful only for a customer-attached bucket.
            prefix="",
            backend=backend,
            config=config,
        )
        try:
            client.create_bucket(bucket)
            client.validate_bucket_access(bucket)
        except Exception as exc:
            msg = f"unable to create workspace storage bucket {bucket!r}: {exc}"
            raise WorkspaceStorageError(msg) from exc
        updated = self.set_workspace_storage(workspace_record.id, storage)
        self._invalidate_token_cache_if_present(token_id_for_cache_invalidation)
        return updated

    def attach_external_workspace_storage(
        self,
        workspace: str,
        storage: WorkspaceStorageConfig,
        *,
        actor_workspace_id: str | None = None,
        token_id_for_cache_invalidation: str | None = None,
    ) -> WorkspaceRecord:
        workspace_record = self.get_workspace(workspace)
        self._validate_storage_attach_allowed(
            workspace_record,
            actor_workspace_id=actor_workspace_id,
        )
        if not storage.bucket:
            msg = "workspace storage bucket is required"
            raise WorkspaceStorageError(msg)
        client = self._workspace_storage_client_for(storage)
        validation_error: Exception | None = None
        try:
            client.validate_bucket_access(storage.bucket)
        except Exception as exc:
            validation_error = exc
        close_error: Exception | None = None
        try:
            client.close()
        except Exception as exc:
            close_error = exc
        if validation_error is not None:
            msg = f"unable to access workspace storage bucket {storage.bucket!r}"
            if close_error is not None:
                raise WorkspaceStorageError(
                    f"{msg}; client cleanup also failed"
                ) from ExceptionGroup(
                    "workspace storage validation and client cleanup failed",
                    [validation_error, close_error],
                )
            raise WorkspaceStorageError(f"{msg}: {validation_error}") from validation_error
        if close_error is not None:
            raise WorkspaceStorageError(
                f"unable to close workspace storage client for bucket {storage.bucket!r}"
            ) from close_error
        updated = self.set_workspace_storage(workspace_record.id, storage)
        self._invalidate_token_cache_if_present(token_id_for_cache_invalidation)
        return updated

    def _validate_storage_attach_allowed(
        self,
        workspace: WorkspaceRecord,
        *,
        actor_workspace_id: str | None = None,
    ) -> None:
        if actor_workspace_id is not None and actor_workspace_id != workspace.id:
            msg = "invalid token for workspace"
            raise WorkspaceStorageAuthorizationError(msg)
        if _workspace_storage_available(workspace.storage):
            msg = "workspace storage already exists"
            raise WorkspaceStorageAlreadyExistsError(msg)

    def _default_workspace_storage(
        self,
        *,
        client: WorkspaceBucketClient,
        bucket: str,
        prefix: str,
        backend: str,
        config: dict[str, JsonValue] | None,
    ) -> WorkspaceStorageConfig:
        settings = _workspace_bucket_settings(client)
        if settings.session_token:
            raise WorkspaceStorageError(
                "temporary platform credentials cannot own durable workspace storage"
            )
        default_config: dict[str, JsonValue] = {
            "endpoint_url": settings.endpoint_url or "",
            "region": settings.region_name,
            "force_path_style": settings.force_path_style,
            "access_key": settings.access_key_id,
            "secret_key": settings.secret_access_key,
        }
        default_config.update(config or {})
        return WorkspaceStorageConfig(
            backend=backend,
            bucket=bucket,
            prefix=prefix,
            config=default_config,
        )

    def _default_workspace_storage_client(self) -> WorkspaceBucketClient:
        if self.workspace_storage_client is None:
            msg = "workspace storage client is required to create workspace storage"
            raise WorkspaceStorageError(msg)
        return self.workspace_storage_client

    def _workspace_storage_client_for(
        self,
        storage: WorkspaceStorageConfig,
    ) -> OwnedWorkspaceBucketClient:
        if self.workspace_storage_client_factory is not None:
            return self.workspace_storage_client_factory(storage)
        msg = "workspace storage client factory is required to validate external workspace storage"
        raise WorkspaceStorageError(msg)

    def _invalidate_token_cache_if_present(self, token_id: str | None) -> None:
        _ = token_id
        return

    def export_workspace_config(
        self,
        workspace: str,
        *,
        http_host: str = "127.0.0.1",
        http_port: int = 9000,
        http_tls: bool = False,
        grpc_host: str = "127.0.0.1",
        grpc_port: int = 9001,
        grpc_tls: bool = False,
    ) -> WorkspaceConfigExport:
        workspace_record = self.get_workspace(workspace)
        return WorkspaceConfigExport(
            gateway_http_host=http_host,
            gateway_http_port=http_port,
            gateway_http_tls=http_tls,
            gateway_grpc_host=grpc_host,
            gateway_grpc_port=grpc_port,
            gateway_grpc_tls=grpc_tls,
            workspace_id=workspace_record.id,
        )

    def list_workspaces(
        self,
        *,
        include_deleted: bool = False,
        include_deleting: bool = False,
    ) -> list[WorkspaceRecord]:
        with self.context.database.session() as session:
            records = _workspace_records(session).list()
        if not include_deleted:
            visible_statuses = {WorkspaceStatus.Active}
            if include_deleting:
                visible_statuses.add(WorkspaceStatus.Deleting)
            records = [item for item in records if item.status in visible_statuses]
        records.sort(key=lambda item: item.name)
        return records

    def get_workspace(self, workspace_id_or_name: str = "default") -> WorkspaceRecord:
        with self.context.database.session() as session:
            return self.context.workspace(session, workspace_id_or_name)

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
        workspace_record = self.get_workspace(workspace)
        with self.context.database.session() as session:
            repository = _stub_records(session)
            stubs = repository.list(workspace_id=workspace_record.id)
            existing = _stub_by_name_and_app(stubs, name, app_id=app_id) if reuse_existing else None
            if existing is not None and deployment_id is None:
                existing = next(
                    (
                        item
                        for item in stubs
                        if item.name == name
                        and item.app_id == app_id
                        and item.deployment_id is None
                    ),
                    None,
                )
            now = utc_now()
            metadata_payload = dict(metadata) if metadata is not None else {}
            config_payload = (
                config
                if isinstance(config, StubConfig)
                else StubConfig.model_validate(dict(config) if config is not None else {})
            )
            if existing is None:
                ArtifactCleanupRepository(session).assert_stub_config_available(
                    config_payload,
                    workspace_id=workspace_record.id,
                    metadata=metadata_payload,
                )
                record = repository.create(
                    {
                        "workspace_id": workspace_record.id,
                        "name": name,
                        "kind": kind.value,
                        "handler": handler,
                        "deployment_id": deployment_id,
                        "app_id": app_id,
                        "public": public,
                        "config": config_payload,
                        "metadata": metadata_payload,
                        "created_at": now,
                        "updated_at": now,
                    },
                    workspace_id=workspace_record.id,
                    name=name,
                )
                change = WorkspaceChangeType.Created
            else:
                existing.kind = kind
                existing.handler = handler or existing.handler
                existing.deployment_id = deployment_id or existing.deployment_id
                existing.app_id = app_id or existing.app_id
                existing.public = public
                existing_config = _stub_config_payload(existing.config)
                existing_config.update(_stub_config_payload(config_payload))
                existing.config = StubConfig.model_validate(existing_config)
                ArtifactCleanupRepository(session).assert_stub_config_available(
                    existing.config,
                    workspace_id=workspace_record.id,
                    metadata=existing.metadata,
                )
                existing.metadata.update(metadata_payload)
                existing.updated_at = now
                record = repository.upsert(
                    existing,
                    workspace_id=workspace_record.id,
                    name=name,
                )
                change = WorkspaceChangeType.Updated
            app = (
                AppRepository(session).get(record.app_id, workspace_id=record.workspace_id)
                if record.app_id is not None
                else None
            )
            rewrite_persisted_sandbox_url_visibility(
                session,
                stub=record,
                app_public=bool(app and app.public),
            )
        self._publish_stub_change(record, change)
        return record

    def get_stub(self, stub_id_or_name: str, *, workspace: str | None = None) -> StubRecord:
        workspace_id = self.get_workspace(workspace).id if workspace is not None else None
        with self.context.database.session() as session:
            repository = _stub_records(session)
            stub_id = try_uuid(stub_id_or_name)
            if stub_id is not None:
                record = (
                    repository.get(stub_id, workspace_id=workspace_id)
                    if workspace_id is not None
                    else repository.get_across_workspaces(stub_id)
                )
                if record is not None:
                    return record
            records = (
                repository.list(workspace_id=workspace_id)
                if workspace_id is not None
                else repository.list_across_workspaces()
            )
        record = _stub_by_name(records, stub_id_or_name)
        if record is None:
            msg = f"stub not found: {stub_id_or_name}"
            raise NotFoundError(msg)
        return record

    def list_stubs(
        self,
        *,
        workspace: str | None = None,
        app_id: str | None = None,
    ) -> list[StubRecord]:
        workspace_id = self.get_workspace(workspace).id if workspace is not None else None
        with self.context.database.session() as session:
            repository = _stub_records(session)
            records = (
                repository.list(workspace_id=workspace_id, app_id=app_id)
                if workspace_id is not None
                else repository.list_across_workspaces(app_id=app_id)
            )
        records.sort(key=lambda item: (item.workspace_id, item.name))
        return records

    def discard_deployment_registration_stub(
        self,
        stub_id: str,
        *,
        deployment_id: str,
        workspace: str = "default",
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
                msg = f"deployment registration stub could not be discarded: {stub.id}"
                raise ConflictError(msg)
        self._publish_stub_change(stub, WorkspaceChangeType.Deleted)

    def get_stub_config(
        self,
        stub_id_or_name: str,
    ) -> dict[str, JsonValue]:
        stub = self.get_stub(stub_id_or_name)
        if not stub.public:
            msg = f"stub config is not public: {stub_id_or_name}"
            raise PermissionError(msg)
        return _limited_public_config(stub.config)

    def update_stub_config(
        self,
        stub_id_or_name: str,
        *,
        workspace: str = "default",
        fields: Mapping[str, StubConfigUpdateValue],
    ) -> StubConfigUpdateResult:
        if not fields:
            msg = "at least one config field is required"
            raise InvalidInputError(msg)
        stub = self.get_stub(stub_id_or_name, workspace=workspace)
        config = _stub_config_payload(stub.config)
        for field_path, value in fields.items():
            _set_nested_config_value(config, field_path, value)
        stub.config = StubConfig.model_validate(config)
        stub.updated_at = utc_now()
        with self.context.database.session() as session:
            updated_stub = _stub_records(session).upsert(
                stub,
                workspace_id=stub.workspace_id,
                name=stub.name,
            )
        self._publish_stub_change(updated_stub, WorkspaceChangeType.Updated)
        updated = tuple(sorted(fields))
        return StubConfigUpdateResult(
            stub=updated_stub,
            updated_fields=updated,
            message=f"stub config updated successfully: {', '.join(updated)}",
        )

    def clone_stub(
        self,
        stub_id_or_name: str,
        *,
        apps: AppRegistry,
        workspace: str = "default",
        overrides: StubCloneOverride | None = None,
    ) -> StubCloneResult:
        source = self.get_stub(stub_id_or_name)
        target_workspace = self.get_workspace(workspace)
        if not source.public and source.workspace_id != target_workspace.id:
            msg = f"stub cannot be cloned from workspace: {source.id}"
            raise PermissionError(msg)
        config = _stub_config_payload(source.config)
        if overrides is not None:
            runtime_config = config.get("runtime")
            if not isinstance(runtime_config, dict):
                runtime_config = {}
                config["runtime"] = runtime_config
            if overrides.cpu is not None:
                runtime_config["cpu"] = overrides.cpu
            if overrides.memory is not None:
                runtime_config["memory"] = overrides.memory
            if overrides.gpu is not None:
                runtime_config["gpu"] = overrides.gpu
            if overrides.gpu_count is not None:
                runtime_config["gpu_count"] = overrides.gpu_count
        cloned = self.create_stub(
            source.name,
            workspace=target_workspace.id,
            kind=source.kind,
            handler=source.handler,
            deployment_id=source.deployment_id,
            public=source.public,
            config=config,
            metadata={**source.metadata, "source_stub_id": source.id},
        )
        copied_objects = self._copy_stub_objects(source, cloned, target_workspace)
        if copied_objects:
            object_remap = {source_id: target_id for source_id, target_id in copied_objects}
            config = _replace_config_object_references(config, object_remap)
            cloned.config = StubConfig.model_validate(config)
            cloned.metadata["source_stub_id"] = source.id
            cloned.metadata["copied_object_ids"] = [target_id for _, target_id in copied_objects]
            cloned.updated_at = utc_now()
            with self.context.database.session() as session:
                cloned = _stub_records(session).upsert(
                    cloned,
                    workspace_id=cloned.workspace_id,
                    name=cloned.name,
                )
        config = self._remap_clone_runtime_config(config, target_workspace)
        cloned.config = StubConfig.model_validate(config)
        cloned.updated_at = utc_now()
        with self.context.database.session() as session:
            cloned = _stub_records(session).upsert(
                cloned,
                workspace_id=cloned.workspace_id,
                name=cloned.name,
            )
        self._publish_stub_change(cloned, WorkspaceChangeType.Updated)
        app = apps.create(
            source.name,
            stub_id=cloned.id,
            workspace=target_workspace.id,
            public=source.public,
            metadata={"source_stub_id": source.id},
        )
        return StubCloneResult(
            source_stub=source,
            cloned_stub=cloned,
            app=app,
            copied_config=_masked_config_object(config),
            copied_objects=tuple(target_id for _, target_id in copied_objects),
        )

    def _copy_stub_objects(
        self,
        source: StubRecord,
        cloned: StubRecord,
        target_workspace: WorkspaceRecord,
    ) -> list[tuple[str, str]]:
        with self.context.database.session() as session:
            repository = ObjectRepository(session)
            source_objects = [
                item
                for item in repository.list(workspace_id=source.workspace_id)
                if item.metadata.get("stub_id") == source.id or item.key.endswith(source.id)
            ]
        copied: list[tuple[str, str]] = []
        for source_object in source_objects:
            source_path = _local_object_path(source_object.path)
            if source_path is None:
                msg = (
                    "stub clone object copy requires live storage for non-local object "
                    f"{source_object.bucket}/{source_object.key}"
                )
                raise ValueError(msg)
            if not source_path.is_file():
                msg = f"stub clone source object file not found: {source_object.path}"
                raise FileNotFoundError(msg)
            target_object_id = str(uuid4())
            target_key = f"clones/{cloned.id}/{Path(source_object.key).name or target_object_id}"
            target_path = (
                self.context.paths.root / "objects" / target_workspace.id / target_object_id
            )
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_path, target_path)
            metadata = {"stub_id": cloned.id, "workspace_id": target_workspace.id}
            target_object = ObjectRecord(
                id=target_object_id,
                bucket=source_object.bucket,
                key=target_key,
                path=str(target_path),
                size=source_object.size,
                sha256=source_object.sha256,
                content_type=source_object.content_type,
                metadata=metadata,
            )
            with self.context.database.session() as session:
                target_object = ObjectRepository(session).upsert(
                    target_object,
                    workspace_id=target_workspace.id,
                )
            copied.append((source_object.id, target_object.id))
        return copied

    def _remap_clone_runtime_config(
        self,
        config: dict[str, JsonValue],
        target_workspace: WorkspaceRecord,
    ) -> dict[str, JsonValue]:
        cloned = deepcopy(config)
        with self.context.database.session() as session:
            secret_repository = SecretRepository(session)
            volume_repository = VolumeRepository(session)
            secrets = cloned.get("secrets")
            if isinstance(secrets, list):
                remapped_secrets: list[JsonValue] = []
                for item in secrets:
                    secret_name = item.get("name") if isinstance(item, dict) else item
                    if not isinstance(secret_name, str):
                        continue
                    if secret_repository.get(secret_name, workspace_id=target_workspace.id) is None:
                        continue
                    remapped_secrets.append(item)
                cloned["secrets"] = remapped_secrets
            volumes = cloned.get("volumes")
            if isinstance(volumes, list):
                remapped_volumes: list[JsonValue] = []
                for item in volumes:
                    if not isinstance(item, dict):
                        continue
                    volume_name = item.get("name") or item.get("id")
                    if not isinstance(volume_name, str) or not volume_name:
                        continue
                    volume = volume_repository.get(volume_name, workspace_id=target_workspace.id)
                    if volume is None:
                        volume = volume_repository.create(
                            volume_name,
                            workspace_id=target_workspace.id,
                        )
                    remapped: dict[str, JsonValue] = {
                        **item,
                        "name": volume.name,
                        "id": volume.id,
                    }
                    remapped.pop("path", None)
                    remapped_volumes.append(remapped)
                cloned["volumes"] = remapped_volumes
        return cloned

    def stub_url(
        self,
        stub_id_or_name: str,
        *,
        apps: AppReader,
        workspace: str | None = None,
        external_url: str = "http://127.0.0.1:9000",
        mode: InvokeUrlMode = InvokeUrlMode.Path,
        deployment_id: str | None = None,
        port: int | None = None,
    ) -> StubUrlPlan:
        stub = self.get_stub(stub_id_or_name, workspace=workspace)
        deployment = self._deployment(deployment_id or stub.deployment_id or "")
        app = apps.get(stub.app_id, workspace=workspace) if stub.app_id else None
        ports = _stub_ports(stub, port=port)
        deployment_subdomain = app.name if app is not None else ""
        target = StubUrlTarget(
            kind=stub.kind.value,
            stub_id=stub.id,
            deployment_name=deployment.name if deployment else stub.name,
            deployment_version=deployment.version if deployment else 1,
            deployment_subdomain=deployment_subdomain,
            public=stub.public or bool(app and app.public),
            ports=ports,
        )
        try:
            if stub.kind is StubKind.Pod:
                url = build_pod_url(external_url, mode, target)
            elif stub.kind is StubKind.Sandbox:
                raise InvalidInputError("sandbox URLs require a container-specific exposure")
            elif deployment is not None:
                url = build_deployment_url(external_url, mode, target)
            else:
                url = build_stub_url(external_url, mode, target)
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc
        return StubUrlPlan(
            stub=stub,
            url=url,
            mode=mode,
            external_url=external_url,
            route_kind=stub.kind,
            deployment=deployment,
        )

    def upsert_concurrency_limit(
        self,
        name: str,
        *,
        limit: int,
        workspace: str = "default",
        resource_type: str = DEFAULT_RESOURCE_TYPE,
        resource_id: str | None = None,
        metadata: Mapping[str, JsonValue] | None = None,
    ) -> ConcurrencyLimitRecord:
        workspace_record = self.get_workspace(workspace)
        with self.context.database.session() as session:
            repository = _limit_records(session)
            existing = _limit_by_name(repository.list(workspace_id=workspace_record.id), name)
            now = utc_now()
            metadata_payload = dict(metadata) if metadata is not None else {}
            if existing is None:
                record = repository.create(
                    {
                        "workspace_id": workspace_record.id,
                        "name": name,
                        "limit": limit,
                        "in_flight": 0,
                        "resource_type": resource_type,
                        "resource_id": resource_id,
                        "metadata": metadata_payload,
                        "created_at": now,
                        "updated_at": now,
                    },
                    workspace_id=workspace_record.id,
                    name=name,
                    status=resource_type,
                )
            else:
                existing.limit = limit
                existing.resource_type = resource_type
                existing.resource_id = resource_id
                existing.metadata.update(metadata_payload)
                existing.in_flight = min(existing.in_flight, existing.limit)
                existing.updated_at = now
                record = repository.upsert(
                    existing,
                    workspace_id=workspace_record.id,
                    name=name,
                    status=resource_type,
                )
            workspace_record.concurrency_limit_id = record.id
            workspace_record.updated_at = now
            _workspace_records(session).upsert(
                workspace_record,
                name=workspace_record.name,
                status=workspace_record.status.value,
            )
        self._publish_concurrency_change(
            record,
            WorkspaceChangeType.Created if existing is None else WorkspaceChangeType.Updated,
        )
        return record

    def create_concurrency_limit(
        self,
        name: str,
        *,
        limit: int,
        workspace: str = "default",
        resource_type: str = DEFAULT_RESOURCE_TYPE,
        resource_id: str | None = None,
        metadata: Mapping[str, JsonValue] | None = None,
    ) -> ConcurrencyLimitRecord:
        workspace_record = self.get_workspace(workspace)
        now = utc_now()
        metadata_payload = dict(metadata) if metadata is not None else {}
        with self.context.database.session() as session:
            repository = _limit_records(session)
            record = repository.create(
                {
                    "workspace_id": workspace_record.id,
                    "name": name,
                    "limit": limit,
                    "in_flight": 0,
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                    "metadata": metadata_payload,
                    "created_at": now,
                    "updated_at": now,
                },
                workspace_id=workspace_record.id,
                name=name,
                status=resource_type,
            )
            workspace_record.concurrency_limit_id = record.id
            workspace_record.updated_at = now
            _workspace_records(session).upsert(
                workspace_record,
                name=workspace_record.name,
                status=workspace_record.status.value,
            )
        self._publish_concurrency_change(record, WorkspaceChangeType.Created)
        return record

    def list_concurrency_limits(
        self,
        *,
        workspace: str | None = None,
    ) -> list[ConcurrencyLimitRecord]:
        workspace_id = self.get_workspace(workspace).id if workspace is not None else None
        with self.context.database.session() as session:
            repository = _limit_records(session)
            records = (
                repository.list(workspace_id=workspace_id)
                if workspace_id is not None
                else repository.list_across_workspaces()
            )
        records.sort(key=lambda item: (item.workspace_id, item.name, item.created_at))
        return records

    def current_concurrency_limit(self, *, workspace: str = "default") -> ConcurrencyLimitRecord:
        workspace_record = self.get_workspace(workspace)
        if workspace_record.concurrency_limit_id is None:
            msg = f"current concurrency limit not found for workspace: {workspace}"
            raise NotFoundError(msg)
        with self.context.database.session() as session:
            record = _limit_records(session).get(
                workspace_record.concurrency_limit_id,
                workspace_id=workspace_record.id,
            )
        if record is None:
            msg = f"current concurrency limit not found for workspace: {workspace}"
            raise NotFoundError(msg)
        return record

    def delete_current_concurrency_limit(self, *, workspace: str = "default") -> None:
        workspace_record = self.get_workspace(workspace)
        previous_limit_id = workspace_record.concurrency_limit_id
        workspace_record.concurrency_limit_id = None
        workspace_record.updated_at = utc_now()
        with self.context.database.session() as session:
            _workspace_records(session).upsert(
                workspace_record,
                name=workspace_record.name,
                status=workspace_record.status.value,
            )
        if self.workspace_changes is not None and previous_limit_id is not None:
            self.workspace_changes.emit_change(
                workspace_id=workspace_record.id,
                topic=WorkspaceChangeTopic.Concurrency,
                change=WorkspaceChangeType.Deleted,
                resource_id=previous_limit_id,
            )

    def revert_concurrency_limit(self, *, workspace: str = "default") -> ConcurrencyLimitRecord:
        workspace_record = self.get_workspace(workspace)
        limits = [
            item
            for item in self.list_concurrency_limits(workspace=workspace_record.id)
            if item.id != workspace_record.concurrency_limit_id and item.limit > 0
        ]
        if not limits:
            msg = "no previous non-zero concurrency limit found"
            raise NotFoundError(msg)
        limits.sort(key=lambda item: item.created_at, reverse=True)
        chosen = limits[0]
        workspace_record.concurrency_limit_id = chosen.id
        workspace_record.updated_at = utc_now()
        with self.context.database.session() as session:
            _workspace_records(session).upsert(
                workspace_record,
                name=workspace_record.name,
                status=workspace_record.status.value,
            )
        self._publish_concurrency_change(chosen, WorkspaceChangeType.Updated)
        return chosen

    def acquire_concurrency(
        self,
        limit_id_or_name: str,
        *,
        workspace: str = "default",
    ) -> ConcurrencyAcquireResult:
        return self._change_concurrency(limit_id_or_name, workspace=workspace, delta=1)

    def release_concurrency(
        self,
        limit_id_or_name: str,
        *,
        workspace: str = "default",
    ) -> ConcurrencyAcquireResult:
        return self._change_concurrency(limit_id_or_name, workspace=workspace, delta=-1)

    def list_sandbox_rows(
        self,
        *,
        workspace: str = "default",
        app_id: str | None = None,
        limit: int = 50,
    ) -> SandboxListResponse:
        workspace_record = self.get_workspace(workspace)
        sandbox_stubs = [
            stub
            for stub in self.list_stubs(workspace=workspace_record.id)
            if stub.kind is StubKind.Sandbox and (app_id is None or stub.app_id == app_id)
        ]
        with self.context.database.session() as session:
            containers = ContainerRepository(session).records.list(workspace_id=workspace_record.id)
        rows: list[SandboxRow] = []
        for stub in sandbox_stubs:
            container = _container_for_stub(containers, stub)
            created_at = stub.created_at
            rows.append(
                SandboxRow(
                    id=stub.id,
                    stub_id=stub.id,
                    name=stub.name,
                    created_at=created_at,
                    status=_sandbox_status_for_stub(stub, container=container),
                    gpu=stub.config.runtime.gpu,
                    container_id=container.id if container else None,
                    time_to_started_ms=_duration_ms(created_at, container.started_at)
                    if container
                    else None,
                    lifetime_ms=_duration_ms(
                        _container_started_at(container),
                        container.finished_at,
                    )
                    if container
                    else None,
                )
            )
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return SandboxListResponse(data=tuple(rows[: max(min(limit, 200), 1)]))

    def sandbox_stats(
        self,
        *,
        workspace: str = "default",
        app_id: str | None = None,
    ) -> SandboxStatsResponse:
        rows = self.list_sandbox_rows(workspace=workspace, app_id=app_id, limit=200).data
        status_counts = {status: 0 for status in SandboxDashboardStatus}
        concurrent = 0
        for row in rows:
            status_counts[row.status] += 1
            if row.status in {
                SandboxDashboardStatus.Pending,
                SandboxDashboardStatus.Running,
                SandboxDashboardStatus.Stopping,
            }:
                concurrent += 1
        buckets: dict[datetime, int] = {}
        for row in rows:
            day = datetime.combine(row.created_at.date(), time.min, tzinfo=UTC)
            buckets[day] = buckets.get(day, 0) + 1
        return SandboxStatsResponse(
            concurrent=concurrent,
            total_created=len(rows),
            rate_per_second=0.0,
            status_counts=status_counts,
            created_buckets=tuple(
                SandboxCreatedBucket(timestamp=day, count=count)
                for day, count in sorted(buckets.items())
            ),
        )

    def sandbox_timeline(
        self,
        stub_id_or_name: str,
        *,
        workspace: str = "default",
        container_id: str | None = None,
    ) -> SandboxTimeline:
        stub = self.get_stub(stub_id_or_name, workspace=workspace)
        if stub.kind is not StubKind.Sandbox:
            msg = f"stub is not a sandbox: {stub_id_or_name}"
            raise InvalidInputError(msg)
        with self.context.database.session() as session:
            containers = ContainerRepository(session).records.list(workspace_id=stub.workspace_id)
        container = (
            next((item for item in containers if item.id == container_id), None)
            if container_id
            else _container_for_stub(containers, stub)
        )
        created_at = stub.created_at
        started_at = _container_started_at(container)
        ended_at = _container_finished_at(container)
        return SandboxTimeline(
            container_id=container.id if container else container_id,
            status=_sandbox_status_for_stub(stub, container=container),
            created_at=created_at,
            started_at=started_at,
            ended_at=ended_at,
            startup_ms=_duration_ms(created_at, container.started_at) if container else None,
            runtime_ms=_duration_ms(started_at, container.finished_at) if container else None,
        )

    def _deployment(self, deployment_id: str) -> Deployment | None:
        """Operator/system resolution by id or name across workspaces."""
        if not deployment_id:
            return None
        with self.context.database.session() as session:
            repository = DeploymentRepository(session)
            deployment = repository.get_across_workspaces(deployment_id)
            if deployment is not None:
                return deployment
            matches = [
                item
                for item in repository.list_across_workspaces()
                if item.name == deployment_id and item.active
            ]
        return max(matches, key=lambda item: item.version) if matches else None

    def _publish_stub_change(
        self,
        stub: StubRecord,
        change: WorkspaceChangeType,
    ) -> None:
        if self.workspace_changes is None:
            return
        self.workspace_changes.emit_change(
            workspace_id=stub.workspace_id,
            topic=WorkspaceChangeTopic.Workloads,
            change=change,
            resource_id=stub.id,
            app_id=stub.app_id,
            deployment_id=stub.deployment_id,
            stub_id=stub.id,
        )

    def _publish_concurrency_change(
        self,
        record: ConcurrencyLimitRecord,
        change: WorkspaceChangeType,
    ) -> None:
        if self.workspace_changes is None:
            return
        self.workspace_changes.emit_change(
            workspace_id=record.workspace_id,
            topic=WorkspaceChangeTopic.Concurrency,
            change=change,
            resource_id=record.id,
        )

    def _change_concurrency(
        self,
        limit_id_or_name: str,
        *,
        workspace: str,
        delta: int,
    ) -> ConcurrencyAcquireResult:
        workspace_record = self.get_workspace(workspace)
        with self.context.database.session() as session:
            repository = _limit_records(session)
            record = _limit_by_id_or_name(
                repository,
                limit_id_or_name,
                workspace_id=workspace_record.id,
            )
            before = record.available
            if delta > 0 and record.saturated:
                return ConcurrencyAcquireResult(
                    status=ConcurrencyAcquireStatus.Saturated,
                    acquired=False,
                    record=record,
                    available_before=before,
                    available_after=record.available,
                    reason="concurrency limit is saturated",
                )
            if delta < 0 and record.in_flight == 0:
                return ConcurrencyAcquireResult(
                    status=ConcurrencyAcquireStatus.Released,
                    acquired=False,
                    record=record,
                    available_before=before,
                    available_after=record.available,
                    reason="no concurrency slot was held",
                )
            if delta > 0:
                record.in_flight += 1
                status = ConcurrencyAcquireStatus.Acquired
                acquired = True
                reason = "slot acquired"
            else:
                record.in_flight = max(record.in_flight - 1, 0)
                status = ConcurrencyAcquireStatus.Released
                acquired = False
                reason = "slot released"
            record.updated_at = utc_now()
            record = repository.upsert(
                record,
                workspace_id=record.workspace_id,
                name=record.name,
                status=record.resource_type,
            )
            result = ConcurrencyAcquireResult(
                status=status,
                acquired=acquired,
                record=record,
                available_before=before,
                available_after=record.available,
                reason=reason,
            )
        self._publish_concurrency_change(record, WorkspaceChangeType.Updated)
        return result


def _workspace_by_name(records: list[WorkspaceRecord], name: str) -> WorkspaceRecord | None:
    return next((item for item in records if item.name == name), None)


def _workspace_storage_available(storage: WorkspaceStorageConfig) -> bool:
    return bool(storage.bucket and storage.backend != "local")


def _stub_by_name(records: list[StubRecord], name: str) -> StubRecord | None:
    return next((item for item in records if item.name == name), None)


def _stub_by_name_and_app(
    records: list[StubRecord],
    name: str,
    *,
    app_id: str | None,
) -> StubRecord | None:
    return next((item for item in records if item.name == name and item.app_id == app_id), None)


def _limit_by_name(
    records: list[ConcurrencyLimitRecord],
    name: str,
) -> ConcurrencyLimitRecord | None:
    return next((item for item in records if item.name == name), None)


def _limit_by_id_or_name(
    repository: WorkspaceTableRepository[ConcurrencyLimitRecord],
    limit_id_or_name: str,
    *,
    workspace_id: str,
) -> ConcurrencyLimitRecord:
    limit_id = try_uuid(limit_id_or_name)
    if limit_id is not None:
        record = repository.get(limit_id, workspace_id=workspace_id)
        if record is not None:
            return record
    record = _limit_by_name(repository.list(workspace_id=workspace_id), limit_id_or_name)
    if record is None:
        msg = f"concurrency limit not found: {limit_id_or_name}"
        raise NotFoundError(msg)
    return record


def _stub_ports(stub: StubRecord, *, port: int | None = None) -> list[int]:
    if port is not None:
        return [port]
    return list(stub.config.ports.values()) or list(stub.config.runtime.ports.values())


def _container_for_stub(
    containers: list[ContainerRecord],
    stub: StubRecord,
) -> ContainerRecord | None:
    candidates = [
        item
        for item in containers
        if item.env.get(STUB_ID_ENV) == stub.id or item.name == stub.name
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda item: item.started_at or item.created_at, reverse=True)
    return candidates[0]


def _container_started_at(container: ContainerRecord | None) -> datetime | None:
    return container.started_at if container else None


def _container_finished_at(container: ContainerRecord | None) -> datetime | None:
    return container.finished_at if container else None


def _sandbox_status_for_stub(
    stub: StubRecord,
    *,
    container: ContainerRecord | None,
) -> SandboxDashboardStatus:
    if container is not None:
        if container.status is ContainerStatus.Running:
            return SandboxDashboardStatus.Running
        if container.status is ContainerStatus.Pending:
            return SandboxDashboardStatus.Pending
        if container.status is ContainerStatus.Failed:
            return SandboxDashboardStatus.Failed
        if container.status is ContainerStatus.Exited:
            return SandboxDashboardStatus.Stopped
    status = stub.config.status
    if status:
        try:
            return SandboxDashboardStatus(status.lower())
        except ValueError:
            return SandboxDashboardStatus.Stopped
    return SandboxDashboardStatus.Stopped


def _duration_ms(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    milliseconds = int((end - start).total_seconds() * 1000)
    return max(milliseconds, 0)


__all__ = [
    "ControlPlaneService",
    "WorkspaceStorageAlreadyExistsError",
    "WorkspaceStorageAuthorizationError",
    "WorkspaceStorageError",
]
