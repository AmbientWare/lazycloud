from __future__ import annotations

from datetime import datetime

from pydantic import Field, JsonValue, field_validator

from shared.enums import StringEnum
from shared.http.base import HttpModel
from shared.identity import WorkspaceRecord, WorkspaceStatus, WorkspaceStorageConfig


class WorkspaceStorageResponse(HttpModel):
    backend: str = "local"
    bucket: str | None = None
    prefix: str = ""


class WorkspaceResponse(HttpModel):
    id: str
    name: str
    status: WorkspaceStatus = WorkspaceStatus.Active
    signing_key_prefix: str | None = None
    primary_token_id: str | None = None
    concurrency_limit_id: str | None = None
    storage: WorkspaceStorageResponse = Field(default_factory=WorkspaceStorageResponse)
    labels: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


def workspace_response(record: WorkspaceRecord) -> WorkspaceResponse:
    """The public view of a workspace, with everything secret projected out.

    One definition, because this is what decides which fields leave the API: a new
    secret on the record has to become invisible everywhere at once, and a second
    copy of the exclusion set is how one surface keeps leaking it.
    """
    return WorkspaceResponse.model_validate(
        record.model_dump(
            mode="json",
            exclude={"signing_key": True, "storage": {"config"}},
        )
    )


class WorkspaceSetRequest(HttpModel):
    name: str = "default"
    storage: WorkspaceStorageResponse = Field(default_factory=WorkspaceStorageResponse)
    signing_key_prefix: str | None = None
    primary_token_id: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class WorkspaceCreateRequest(HttpModel):
    name: str | None = None
    storage: WorkspaceStorageResponse | None = None


class WorkspaceUpdateRequest(HttpModel):
    name: str = Field(min_length=1, max_length=63)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        name = value.strip()
        if not name[0].islower() or not all(
            character.islower() or character.isdigit() or character in {"-", "_"}
            for character in name
        ):
            raise ValueError(
                "workspace name must start with a lowercase letter and contain only "
                "lowercase letters, numbers, hyphens, or underscores"
            )
        return name


class WorkspaceAuditAction(StringEnum):
    AdministratorBootstrapped = "administrator_bootstrapped"
    AdministratorRecovered = "administrator_recovered"
    WorkspaceRenamed = "workspace_renamed"
    WorkspaceDeleted = "workspace_deleted"


class WorkspaceAuditTarget(StringEnum):
    Workspace = "workspace"
    Token = "token"


class WorkspaceAuditEventResponse(HttpModel):
    id: str
    workspace_id: str
    action: WorkspaceAuditAction
    actor_token_id: str | None = None
    actor_user_id: str | None = None
    actor_name: str
    target_type: WorkspaceAuditTarget
    target_id: str
    target_name: str
    summary: str
    previous_value: str | None = None
    new_value: str | None = None
    created_at: datetime


class WorkspaceAuditListResponse(HttpModel):
    data: list[WorkspaceAuditEventResponse] = Field(default_factory=list)
    next: str = ""


class WorkspaceStorageRequest(HttpModel):
    bucket_name: str
    access_key: str
    secret_key: str
    endpoint_url: str
    region: str

    def workspace_storage(self) -> WorkspaceStorageConfig:
        return WorkspaceStorageConfig(
            backend="s3",
            bucket=self.bucket_name,
            config={
                "access_key": self.access_key,
                "secret_key": self.secret_key,
                "endpoint_url": self.endpoint_url,
                "region": self.region,
            },
        )


def workspace_storage_config(value: WorkspaceStorageResponse) -> WorkspaceStorageConfig:
    return WorkspaceStorageConfig(
        backend=value.backend,
        bucket=value.bucket,
        prefix=value.prefix,
    )


class WorkspaceListResponse(HttpModel):
    workspaces: list[WorkspaceResponse] = Field(default_factory=list)


class WorkspaceConfigExportResponse(HttpModel):
    gateway_http_host: str
    gateway_http_port: int
    gateway_http_tls: bool
    gateway_grpc_host: str
    gateway_grpc_port: int
    gateway_grpc_tls: bool
    workspace_id: str
    token: str = ""


__all__ = [
    "WorkspaceAuditAction",
    "WorkspaceAuditEventResponse",
    "WorkspaceAuditListResponse",
    "WorkspaceAuditTarget",
    "WorkspaceConfigExportResponse",
    "WorkspaceCreateRequest",
    "WorkspaceListResponse",
    "WorkspaceResponse",
    "WorkspaceSetRequest",
    "WorkspaceStorageRequest",
    "WorkspaceStorageResponse",
    "WorkspaceUpdateRequest",
    "workspace_response",
]
