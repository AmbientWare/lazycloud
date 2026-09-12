from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import uuid4

from billing.admission import DatabaseBillingAdmission
from database.records.apps import AutoscalingStubRecord, StubKind, StubRecord
from database.repositories.apps import DeploymentRepository, StubRepository
from database.repositories.cleanup import CleanupRepository
from database.repositories.common import (
    GlobalTableRepository,
    TableRepositoryConfig,
    WorkspaceTableRepository,
)
from database.repositories.identity import (
    SecretRepository,
    WorkspaceMemberRepository,
    WorkspaceRepository,
    new_signing_key,
)
from database.repositories.orchestration import AutoscalingTargetRepository, ContainerRepository
from database.repositories.storage import ObjectRepository, VolumeRepository
from database.tables.apps import StubTable
from database.tables.identity import ConcurrencyLimitTable, WorkspaceTable
from foundation.ids import try_uuid
from identity.auth import AuthService
from observability.workspace_changes import WorkspaceChangePublisher
from pydantic import JsonValue, TypeAdapter
from shared.app_identity import DEFAULT_RESOURCE_TYPE
from shared.autoscaler_state import autoscaler_target_kind
from shared.containers import ContainerRecord, ContainerStatus
from shared.contracts import ContractModel
from shared.deployment_records import Deployment
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
    StubUrlTarget,
    build_deployment_url,
    build_pod_url,
    build_stub_url,
)
from shared.workload_config import StubConfig
from sqlalchemy.orm import Session

from control.apps import AppReader, AppRegistry
from control.context import ControlContext
from control.events import publish_workload_change
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


class WorkspaceStorageError(RuntimeError):
    pass


class WorkspaceStorageAlreadyExistsError(ValueError):
    pass


class WorkspaceStorageAuthorizationError(PermissionError):
    pass


class WorkspaceCreationAdmission(Protocol):
    """Whether the account creating a workspace is allowed another one.

    Asked before anything is written, so a refusal leaves no workspace, no
    owner and no storage behind. Stated here as a protocol because
    how many workspaces a plan comes with is a billing term rather than a
    control-plane one.
    """

    def assert_may_create_workspace(self, session: Session, *, owner_user_id: str) -> None: ...


class WorkspaceBucketSettings(Protocol):
    bucket: str
    workspace_bucket_prefix: str
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

    def configure_workspace_bucket(self, bucket: str, *, public_origin: str) -> None: ...


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


def _upsert_workspace_row(
    session: Session,
    name: str,
    *,
    storage: WorkspaceStorageConfig | None = None,
    signing_key_prefix: str | None = None,
    primary_token_id: str | None = None,
    labels: dict[str, str] | None = None,
    metadata: Mapping[str, JsonValue] | None = None,
) -> WorkspaceRecord:
    repository = _workspace_records(session)
    existing = WorkspaceRepository(session).by_name(name)
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
        raise ConflictError(f"workspace is not active: {name}")
    if storage is not None:
        # The response model never carries the connection settings, so a caller naming
        # a bucket cannot resend the credentials that reach it. Keeping the existing
        # bag is what stops a settings update from stranding the objects already there.
        existing.storage = storage.model_copy(
            update={"config": storage.config or existing.storage.config}
        )
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


def _stub_preparation_fingerprint(stub: StubRecord) -> str:
    payload = stub.model_dump(mode="json", exclude={"id", "created_at", "updated_at"})
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


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


def _config_reference_values(value: JsonValue) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {item for entry in value for item in _config_reference_values(entry)}
    if isinstance(value, dict):
        return {item for entry in value.values() for item in _config_reference_values(entry)}
    return set()


def _config_object_references(config: Mapping[str, JsonValue]) -> set[str]:
    """Identifiers a config may point at, matched against real object ids by the caller."""
    return {item for value in config.values() for item in _config_reference_values(value)}


@dataclass(slots=True)
class ControlPlaneService:
    context: ControlContext
    workspace_storage_client: WorkspaceBucketClient | None = None
    public_http_origin: str = ""
    workspace_storage_client_factory: (
        Callable[[WorkspaceStorageConfig], OwnedWorkspaceBucketClient] | None
    ) = None
    workspace_changes: WorkspaceChangePublisher | None = None
    workspace_admission: WorkspaceCreationAdmission = field(
        default_factory=DatabaseBillingAdmission
    )
    """What decides whether the account gets another workspace.

    Carries the production decision rather than being left to each composition
    to supply, because a workspace limit that only the API enforces is one every
    other entry point silently grants.
    """

    def set_workspace(
        self,
        name: str,
        *,
        owner_user_id: str,
        storage: WorkspaceStorageConfig | None = None,
        signing_key_prefix: str | None = None,
        primary_token_id: str | None = None,
        labels: dict[str, str] | None = None,
        metadata: Mapping[str, JsonValue] | None = None,
    ) -> WorkspaceRecord:
        """Write a workspace's settings, and its owner when this call creates it.

        The owner row goes in the same transaction as the workspace row because a
        workspace without one is reachable by nobody and resolves to no compute
        account—there is no useful moment between the two writes.
        """
        if not owner_user_id:
            msg = "a workspace is owned by the account that creates it"
            raise InvalidInputError(msg)
        with self.context.database.session() as session:
            record = _upsert_workspace_row(
                session,
                name,
                storage=storage,
                signing_key_prefix=signing_key_prefix,
                primary_token_id=primary_token_id,
                labels=labels,
                metadata=metadata,
            )
            WorkspaceMemberRepository(session).ensure_owner(
                workspace_id=record.id,
                user_id=owner_user_id,
            )
            return record

    def create_workspace(
        self,
        name: str | None = None,
        *,
        owner_user_id: str,
        storage: WorkspaceStorageConfig | None = None,
    ) -> WorkspaceCreateResult:
        workspace_name = name or f"workspace-{uuid4()}"
        # Only a workspace that does not exist yet is a new one. `set_workspace`
        # adopts an existing name and adds the caller as an owner, and refusing
        # that would lock an account out of workspaces it already holds.
        with self.context.database.session() as session:
            if WorkspaceRepository(session).by_name(workspace_name) is None:
                self.workspace_admission.assert_may_create_workspace(
                    session,
                    owner_user_id=owner_user_id,
                )
        workspace = self.set_workspace(
            workspace_name,
            owner_user_id=owner_user_id,
            storage=storage,
        )
        # Adopting an existing workspace keeps the credential it already has. Minting
        # unconditionally repointed `primary_token_id` at a new token on every call, so
        # asking for a workspace that was already there re-keyed it and left the
        # previous primary behind as a durable credential nobody issued deliberately.
        raw_token = ""
        if not workspace.primary_token_id:
            raw_token, token_record = AuthService(self.context).create_token(
                f"{workspace.name}-primary",
                kind=TokenKind.WorkspacePrimary,
                workspace_id=workspace.id,
            )
            with self.context.database.session() as session:
                workspace = _upsert_workspace_row(
                    session,
                    workspace.name,
                    primary_token_id=token_record.id,
                )
        if storage is None:
            workspace = self.ensure_workspace_storage(workspace.id)
        return WorkspaceCreateResult(
            workspace_id=workspace.id,
            token=raw_token,
            workspace=workspace,
        )

    def ensure_default_workspace(
        self,
        owner_user_id: str,
        preferred_name: str = "",
    ) -> WorkspaceRecord:
        """The workspace an account gets on its first sign-in, provisioned once.

        Called on every sign-in rather than only when the account is new. Workspace
        creation reaches object storage and can fail after the account exists, and
        making this conditional on "was just created" would leave such an account with
        no workspace and no path to ever getting one.

        The preferred name is a starting point, not a reservation. Workspace names are
        globally unique while a provider login is renameable, so a login colliding with
        an existing workspace must not turn somebody's sign-in into a conflict they have
        no way to resolve. They can rename it afterwards.
        """
        with self.context.database.session() as session:
            owned = WorkspaceMemberRepository(session).owned_workspace_ids(owner_user_id)
        if owned:
            return self.get_workspace(owned[0])
        return self.create_workspace(
            self._available_workspace_name(preferred_name, owner_user_id),
            owner_user_id=owner_user_id,
        ).workspace

    def _available_workspace_name(self, preferred: str, owner_user_id: str) -> str:
        fallback = f"workspace-{owner_user_id}"
        candidate = _workspace_name_from(preferred)
        if not candidate:
            return fallback
        with self.context.database.session() as session:
            repository = WorkspaceRepository(session)
            if repository.by_name(candidate) is None:
                return candidate
            for suffix in range(2, 10):
                attempt = f"{candidate}-{suffix}"
                if repository.by_name(attempt) is None:
                    return attempt
        # A name this contended is not worth more round trips to guess at. The account
        # id is unique by construction, and the person can rename it.
        return fallback

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

    def ensure_workspace_storage(self, workspace: str) -> WorkspaceRecord:
        """Provision the workspace's platform bucket idempotently."""
        record = self.get_workspace(workspace)
        if record.storage.bucket:
            return record
        return self.create_workspace_storage(record.id)

    def create_workspace_storage(
        self,
        workspace: str,
        *,
        token_id_for_cache_invalidation: str | None = None,
    ) -> WorkspaceRecord:
        workspace_record = self.get_workspace(workspace)
        self._validate_storage_attach_allowed(workspace_record)
        client = self._default_workspace_storage_client()
        prefix = _workspace_bucket_settings(client).workspace_bucket_prefix
        bucket = f"{prefix}-{workspace_record.id}".replace("_", "-")
        try:
            client.create_bucket(bucket)
            client.validate_bucket_access(bucket)
            client.configure_workspace_bucket(bucket, public_origin=self.public_http_origin)
        except Exception as exc:
            msg = f"unable to create workspace storage bucket {bucket!r}: {exc}"
            raise WorkspaceStorageError(msg) from exc
        storage = self._default_workspace_storage(
            client=client,
            bucket=bucket,
        )
        updated = self.set_workspace_storage(workspace_record.id, storage)
        self._invalidate_token_cache_if_present(token_id_for_cache_invalidation)
        return updated

    def attach_external_workspace_storage(
        self,
        workspace: str,
        storage: WorkspaceStorageConfig,
        *,
        token_id_for_cache_invalidation: str | None = None,
    ) -> WorkspaceRecord:
        workspace_record = self.get_workspace(workspace)
        self._validate_storage_attach_allowed(workspace_record)
        if not storage.bucket:
            msg = "workspace storage bucket is required"
            raise WorkspaceStorageError(msg)
        if not (storage.access_key and storage.secret_key and storage.endpoint_url):
            raise WorkspaceStorageError(
                "external workspace storage requires its own endpoint, access key, and secret key"
            )
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

    def _validate_storage_attach_allowed(self, workspace: WorkspaceRecord) -> None:
        # Who may act on this workspace is settled before the call: the route's
        # workspace dependency resolved and authorized it. A second comparison here
        # could only ask a narrower question, and asked it of a credential that no
        # longer names a workspace at all.
        if _workspace_storage_available(workspace.storage):
            msg = "workspace storage already exists"
            raise WorkspaceStorageAlreadyExistsError(msg)

    def _default_workspace_storage(
        self,
        *,
        client: WorkspaceBucketClient,
        bucket: str,
    ) -> WorkspaceStorageConfig:
        """Persist coordinates only; the issuer supplies temporary workspace credentials."""
        settings = _workspace_bucket_settings(client)
        default_config: dict[str, JsonValue] = {
            "endpoint_url": settings.endpoint_url or "",
            "region": settings.region_name,
            "force_path_style": settings.force_path_style,
        }
        return WorkspaceStorageConfig(
            backend="s3",
            bucket=bucket,
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
        metadata_payload = dict(metadata) if metadata is not None else {}
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
            config=(
                config.model_copy(deep=True)
                if isinstance(config, StubConfig)
                else StubConfig.model_validate(dict(config) if config is not None else {})
            ),
            metadata=metadata_payload,
            created_at=now,
            updated_at=now,
        )
        fingerprint = _stub_preparation_fingerprint(requested)
        with self.context.database.session() as session:
            WorkspaceRepository(session).lock_active_owner(workspace_record.id)
            repository = StubRepository(session)
            existing = (
                repository.find_reusable(
                    workspace_id=workspace_record.id,
                    preparation_fingerprint=fingerprint,
                )
                if reuse_existing
                else None
            )
            if existing is not None and _stub_preparation_fingerprint(existing) != fingerprint:
                repository.set_preparation_fingerprint(
                    existing.id, workspace_id=workspace_record.id, fingerprint=None
                )
                existing = None
            CleanupRepository(session).assert_stub_config_available(
                requested.config,
                workspace_id=workspace_record.id,
                metadata=requested.metadata,
            )
            if existing is None:
                record = repository.upsert(requested)
                if reuse_existing:
                    repository.set_preparation_fingerprint(
                        record.id, workspace_id=workspace_record.id, fingerprint=fingerprint
                    )
            else:
                record = existing
            target_kind = autoscaler_target_kind(record.kind)
            if target_kind is not None:
                AutoscalingTargetRepository(session).activate(
                    stub_id=record.id,
                    workspace_id=record.workspace_id,
                    target_kind=target_kind,
                    due_at=now,
                )
        if existing is None:
            publish_workload_change(self.workspace_changes, record, WorkspaceChangeType.Created)
        return record

    def stub_app_ids(
        self,
        stub_ids: Sequence[str],
        *,
        workspace_id: str,
    ) -> dict[str, str]:
        """Resolve a page of stubs to their apps in one session."""

        with self.context.database.session() as session:
            return StubRepository(session).app_ids_by_id(stub_ids, workspace_id=workspace_id)

    def get_stub(self, stub_id_or_name: str, *, workspace: str | None = None) -> StubRecord:
        with self.context.database.session() as session:
            return self.get_stub_in_session(session, stub_id_or_name, workspace=workspace)

    def get_stub_in_session(
        self,
        session: Session,
        stub_id_or_name: str,
        *,
        workspace: str | None = None,
    ) -> StubRecord:
        workspace_id = (
            self.context.workspace(session, workspace).id if workspace is not None else None
        )
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
        deployed_only: bool = False,
    ) -> list[StubRecord]:
        workspace_id = self.get_workspace(workspace).id if workspace is not None else None
        with self.context.database.session() as session:
            repository = _stub_records(session)
            records = (
                repository.list(workspace_id=workspace_id, app_id=app_id)
                if workspace_id is not None
                else repository.list_across_workspaces(app_id=app_id)
            )
        if deployed_only:
            records = [record for record in records if record.deployment_id is not None]
        records.sort(key=lambda item: (item.workspace_id, item.name))
        return records

    def list_autoscaling_stubs(
        self,
        stub_ids: Sequence[str] | None = None,
    ) -> list[AutoscalingStubRecord]:
        with self.context.database.session() as session:
            return StubRepository(session).list_autoscaling_across_workspaces(stub_ids=stub_ids)

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
        publish_workload_change(self.workspace_changes, stub, WorkspaceChangeType.Deleted)

    def discard_registration_source_stub(
        self,
        stub_id: str,
        *,
        workspace: str = "default",
    ) -> bool:
        """Remove the stub a deployment registration was built from.

        A deploy registers a stub first, to carry the uploaded source and the
        built image, and the registration then writes the durable stub that the
        deployment owns. The first one has done its job at that point: its
        config was copied forward, and nothing refers to it.

        Refused rather than forced if anything does refer to it, or if it turns
        out to own a deployment of its own. A stub someone invoked directly
        before deploying is a stub with tasks against it, and that is a stub
        still in use.
        """

        with self.context.database.session() as session:
            workspace_record = self.context.workspace(session, workspace)
            repository = StubRepository(session)
            stub = repository.get_for_update(stub_id, workspace_id=workspace_record.id)
            if stub is None:
                return False
            if stub.deployment_id:
                return False
            if repository.registration_is_bound(stub.id):
                return False
            if not repository.delete(stub.id, workspace_id=workspace_record.id):
                return False
        publish_workload_change(self.workspace_changes, stub, WorkspaceChangeType.Deleted)
        return True

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
            StubRepository(session).set_preparation_fingerprint(
                stub.id, workspace_id=stub.workspace_id, fingerprint=None
            )
            target_kind = autoscaler_target_kind(updated_stub.kind)
            if target_kind is not None:
                AutoscalingTargetRepository(session).activate(
                    stub_id=updated_stub.id,
                    workspace_id=updated_stub.workspace_id,
                    target_kind=target_kind,
                    due_at=updated_stub.updated_at,
                )
        publish_workload_change(self.workspace_changes, updated_stub, WorkspaceChangeType.Updated)
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
                runtime_config["gpu"] = list(overrides.gpu)
            if overrides.gpu_count is not None:
                runtime_config["gpu_count"] = overrides.gpu_count
        copied_objects = self._copy_stub_objects(source, config=config, target=target_workspace)
        metadata: dict[str, JsonValue] = {**source.metadata, "source_stub_id": source.id}
        if copied_objects:
            config = _replace_config_object_references(
                config,
                {source_id: target_object.id for source_id, target_object in copied_objects},
            )
            metadata["copied_object_ids"] = [
                target_object.id for _, target_object in copied_objects
            ]
        else:
            metadata.pop("copied_object_ids", None)
        config = self._remap_clone_runtime_config(config, target_workspace)
        try:
            cloned = self.create_stub(
                source.name,
                workspace=target_workspace.id,
                kind=source.kind,
                handler=source.handler,
                deployment_id=source.deployment_id,
                public=source.public,
                config=config,
                metadata=metadata,
            )
        except Exception as clone_failure:
            try:
                self._discard_copied_objects(copied_objects, workspace_id=target_workspace.id)
            except Exception as cleanup_failure:
                raise ExceptionGroup(
                    "stub clone failed and copied object cleanup was incomplete",
                    [clone_failure, cleanup_failure],
                ) from None
            raise
        self._own_copied_objects(copied_objects, stub=cloned)
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
            copied_objects=tuple(target_object.id for _, target_object in copied_objects),
        )

    def _copy_stub_objects(
        self,
        source: StubRecord,
        *,
        config: Mapping[str, JsonValue],
        target: WorkspaceRecord,
    ) -> list[tuple[str, ObjectRecord]]:
        """Copy the source objects a clone needs into the target workspace.

        The copies must exist before the cloned stub is created, because stub
        creation rejects a config that references objects the target workspace
        does not own. Copies are therefore keyed by their own identifier and are
        bound to the cloned stub once that stub exists.
        """
        referenced = _config_object_references(config)
        with self.context.database.session() as session:
            repository = ObjectRepository(session)
            source_objects = [
                item
                for item in repository.list(workspace_id=source.workspace_id)
                if item.id in referenced
                or item.metadata.get("stub_id") == source.id
                or item.key.endswith(source.id)
            ]
        copied: list[tuple[str, ObjectRecord]] = []
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
            target_key = (
                f"clones/{target_object_id}/{Path(source_object.key).name or target_object_id}"
            )
            target_path = self.context.paths.root / "objects" / target.id / target_object_id
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_path, target_path)
            target_object = ObjectRecord(
                id=target_object_id,
                bucket=source_object.bucket,
                key=target_key,
                path=str(target_path),
                size=source_object.size,
                sha256=source_object.sha256,
                content_type=source_object.content_type,
                metadata={"workspace_id": target.id},
            )
            with self.context.database.session() as session:
                target_object = ObjectRepository(session).upsert(
                    target_object,
                    workspace_id=target.id,
                )
            copied.append((source_object.id, target_object))
        return copied

    def _own_copied_objects(
        self,
        copied_objects: Sequence[tuple[str, ObjectRecord]],
        *,
        stub: StubRecord,
    ) -> None:
        if not copied_objects:
            return
        with self.context.database.session() as session:
            repository = ObjectRepository(session)
            for _source_id, target_object in copied_objects:
                repository.upsert(
                    target_object.model_copy(
                        update={"metadata": {**target_object.metadata, "stub_id": stub.id}}
                    ),
                    workspace_id=stub.workspace_id,
                )

    def _discard_copied_objects(
        self,
        copied_objects: Sequence[tuple[str, ObjectRecord]],
        *,
        workspace_id: str,
    ) -> None:
        """Undo the copies made for a clone that never became a stub."""
        for _source_id, target_object in copied_objects:
            with self.context.database.session() as session:
                ObjectRepository(session).delete(target_object.id, workspace_id=workspace_id)
            Path(target_object.path).unlink(missing_ok=True)

    def _remap_clone_runtime_config(
        self,
        config: dict[str, JsonValue],
        target_workspace: WorkspaceRecord,
    ) -> dict[str, JsonValue]:
        cloned = deepcopy(config)
        created_volume_names: list[str] = []
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
                        # Unlike the volumes route, this asks billing nothing. A
                        # volume is priced on byte-seconds, so the row a clone
                        # makes is free until something writes to it, and what
                        # writes to it is a container that was admitted itself.
                        # Dropping the volume instead — as the secrets branch
                        # above drops a secret the target lacks — would let the
                        # clone run and silently keep nothing.
                        volume, volume_created = volume_repository.create(
                            volume_name,
                            workspace_id=target_workspace.id,
                        )
                        if volume_created:
                            created_volume_names.append(volume.name)
                    remapped: dict[str, JsonValue] = {
                        **item,
                        "name": volume.name,
                        "id": volume.id,
                    }
                    remapped.pop("path", None)
                    remapped_volumes.append(remapped)
                cloned["volumes"] = remapped_volumes
        # After the session, like every other publish here. A volume a clone
        # brought into existence is one the dashboard has to hear about; without
        # this it appears only when something else refreshes the list.
        for created_name in created_volume_names:
            self._publish_volume_change(target_workspace.id, created_name)
        return cloned

    def _publish_volume_change(self, workspace_id: str, name: str) -> None:
        if self.workspace_changes is None:
            return
        self.workspace_changes.emit_change(
            workspace_id=workspace_id,
            topic=WorkspaceChangeTopic.StorageVolumes,
            change=WorkspaceChangeType.Created,
            resource_id=name,
        )

    def stub_url(
        self,
        stub_id_or_name: str,
        *,
        apps: AppReader,
        workspace: str | None = None,
        external_url: str = "http://127.0.0.1:9000",
        deployment_id: str | None = None,
        port: int | None = None,
    ) -> StubUrlPlan:
        stub = self.get_stub(stub_id_or_name, workspace=workspace)
        deployment = self._deployment(deployment_id or stub.deployment_id or "")
        app = apps.get(stub.app_id, workspace=workspace) if stub.app_id else None
        ports = _stub_ports(stub, port=port)
        target = StubUrlTarget(
            kind=stub.kind.value,
            stub_id=stub.id,
            deployment_name=deployment.name if deployment else stub.name,
            deployment_version=deployment.version if deployment else 1,
            subdomain=deployment.subdomain if deployment else "",
            public=stub.public or bool(app and app.public),
            ports=ports,
        )
        try:
            if stub.kind is StubKind.Pod:
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
        row_limit = max(min(limit, 200), 1)
        sandbox_stubs.sort(key=lambda item: item.created_at, reverse=True)
        sandbox_stubs = sandbox_stubs[:row_limit]
        with self.context.database.session() as session:
            containers = ContainerRepository(session).latest_for_stubs(
                workspace_id=workspace_record.id,
                stub_ids=[stub.id for stub in sandbox_stubs],
            )
        rows: list[SandboxRow] = []
        for stub in sandbox_stubs:
            container = containers.get(stub.id)
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
        return SandboxListResponse(data=tuple(rows))

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
            repository = ContainerRepository(session)
            container = (
                repository.get_for_stub(
                    container_id,
                    workspace_id=stub.workspace_id,
                    stub_id=stub.id,
                )
                if container_id
                else repository.latest_for_stubs(
                    workspace_id=stub.workspace_id,
                    stub_ids=(stub.id,),
                ).get(stub.id)
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


def _workspace_name_from(preferred: str) -> str:
    """A provider login reshaped into something a workspace may be called.

    Workspace names allow lowercase letters, digits, hyphens, and underscores, and
    must start with a letter. A GitHub login satisfies none of that reliably: it can
    carry capitals and may begin with a digit. Returns empty when nothing usable is
    left, which the caller reads as "fall back to the account id".
    """
    lowered = "".join(
        character if (character.isascii() and (character.isalnum() or character in "-_")) else "-"
        for character in preferred.strip().lower()
    ).strip("-")
    if not lowered:
        return ""
    if not lowered[0].isalpha():
        lowered = f"w-{lowered}"
    return lowered[:63].rstrip("-_")


def _workspace_storage_available(storage: WorkspaceStorageConfig) -> bool:
    return bool(storage.bucket and storage.backend != "local")


def _stub_by_name(records: list[StubRecord], name: str) -> StubRecord | None:
    matching = [item for item in records if item.name == name]
    if len(matching) > 1:
        raise ConflictError(f"stub name is ambiguous; use a stub ID: {name}")
    return matching[0] if matching else None


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
    "WorkspaceCreationAdmission",
    "WorkspaceStorageAlreadyExistsError",
    "WorkspaceStorageAuthorizationError",
    "WorkspaceStorageError",
]
