from __future__ import annotations

from pydantic import JsonValue
from shared.errors import InvalidInputError
from shared.http.workspaces import WorkspaceAuditAction, WorkspaceAuditTarget
from shared.identity import (
    AuthTokenRecord,
    ConcurrencyLimitRecord,
    DeviceAuthorizationStatus,
    IdentityProvider,
    PlatformRole,
    TokenKind,
    TokenStatus,
    UserIdentityRecord,
    UserRecord,
    UserStatus,
    WorkspaceInvitationRecord,
    WorkspaceInvitationRole,
    WorkspaceMemberRecord,
    WorkspaceRecord,
    WorkspaceRole,
    WorkspaceStatus,
    WorkspaceStorageConfig,
)
from shared.timestamps import to_utc, to_utc_or_none

from database.records.identity import (
    DeviceAuthorizationRecord,
    SecretStorageRecord,
    WorkspaceAuditRecord,
)
from database.tables.identity import (
    ConcurrencyLimitTable,
    DeviceAuthorizationTable,
    SecretTable,
    TokenTable,
    UserIdentityTable,
    UserTable,
    WorkspaceAuditEventTable,
    WorkspaceInvitationTable,
    WorkspaceMemberTable,
    WorkspaceTable,
)


def concurrency_limit_from_table(row: ConcurrencyLimitTable) -> ConcurrencyLimitRecord:
    return ConcurrencyLimitRecord(
        id=row.id,
        workspace_id=row.workspace_id,
        name=row.name,
        limit=row.limit,
        in_flight=row.in_flight,
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        metadata=dict(row.metadata_json),
        created_at=to_utc(row.created_at),
        updated_at=to_utc(row.updated_at),
    )


def workspace_audit_from_table(row: WorkspaceAuditEventTable) -> WorkspaceAuditRecord:
    return WorkspaceAuditRecord(
        id=row.id,
        workspace_id=row.workspace_id,
        action=WorkspaceAuditAction(row.action),
        actor_token_id=row.actor_token_id,
        actor_user_id=row.actor_user_id,
        actor_name=row.actor_name,
        target_type=WorkspaceAuditTarget(row.target_type),
        target_id=row.target_id,
        target_name=row.target_name,
        summary=row.summary,
        previous_value=row.previous_value,
        new_value=row.new_value,
        created_at=to_utc(row.created_at),
    )


def workspace_record_from_table(row: WorkspaceTable) -> WorkspaceRecord:
    config: dict[str, JsonValue] = {}
    if row.storage_endpoint_url is not None:
        config["endpoint_url"] = row.storage_endpoint_url
    if row.storage_region is not None:
        config["region"] = row.storage_region
    if row.storage_access_key is not None:
        config["access_key"] = row.storage_access_key
    if row.storage_secret_key is not None:
        config["secret_key"] = row.storage_secret_key
    if row.storage_force_path_style is not None:
        config["force_path_style"] = row.storage_force_path_style
    return WorkspaceRecord(
        id=row.id,
        name=row.name,
        status=WorkspaceStatus(row.status),
        signing_key=row.signing_key or "",
        signing_key_prefix=row.signing_key_prefix,
        primary_token_id=row.primary_token_id,
        concurrency_limit_id=row.concurrency_limit_id,
        storage=WorkspaceStorageConfig(
            backend=row.storage_backend,
            bucket=row.storage_bucket,
            prefix=row.storage_prefix,
            config=config,
        ),
        labels=dict(row.labels),
        metadata=dict(row.metadata_json),
        created_at=to_utc(row.created_at),
        updated_at=to_utc(row.updated_at),
    )


def write_workspace_row(row: WorkspaceTable, workspace: WorkspaceRecord) -> None:
    storage = workspace.storage
    if storage.config.keys() - {
        "endpoint_url",
        "region",
        "access_key",
        "secret_key",
        "force_path_style",
    }:
        raise InvalidInputError("workspace storage contains unsupported connection settings")
    row.name = workspace.name
    row.status = workspace.status.value
    row.signing_key = workspace.signing_key
    row.signing_key_prefix = workspace.signing_key_prefix
    row.primary_token_id = workspace.primary_token_id
    row.concurrency_limit_id = workspace.concurrency_limit_id
    row.storage_backend = storage.backend
    row.storage_bucket = storage.bucket
    row.storage_prefix = storage.prefix
    row.storage_endpoint_url = storage.endpoint_url if "endpoint_url" in storage.config else None
    row.storage_region = storage.region if "region" in storage.config else None
    row.storage_access_key = storage.access_key if "access_key" in storage.config else None
    row.storage_secret_key = storage.secret_key if "secret_key" in storage.config else None
    row.storage_force_path_style = (
        storage.force_path_style if "force_path_style" in storage.config else None
    )
    row.labels = dict(workspace.labels)
    row.metadata_json = dict(workspace.metadata)
    row.created_at = workspace.created_at
    row.updated_at = workspace.updated_at


def _optional_id(value: str | None) -> str:
    """A nullable owner column as the empty string every reader already expects."""
    return str(value) if value is not None else ""


def auth_token_record_from_table(row: TokenTable) -> AuthTokenRecord:
    """Map the relational token aggregate without consulting a JSON shadow."""
    return AuthTokenRecord(
        id=str(row.id),
        name=row.name,
        token_hash=row.token_hash,
        prefix=row.prefix,
        kind=TokenKind(row.kind),
        device_login=row.device_login,
        user_id=_optional_id(row.user_id),
        workspace_id=_optional_id(row.workspace_id),
        worker_id=row.worker_id,
        status=TokenStatus(row.status),
        scopes=list(row.scopes),
        reusable=row.reusable,
        disabled_by_admin=row.disabled_by_admin,
        created_at=to_utc(row.created_at),
        last_used_at=to_utc_or_none(row.last_used_at),
        expires_at=to_utc_or_none(row.expires_at),
        revoked_at=to_utc_or_none(row.revoked_at),
    )


def device_authorization_record_from_table(
    row: DeviceAuthorizationTable,
) -> DeviceAuthorizationRecord:
    """Map the relational device authorization without consulting JSON state."""
    return DeviceAuthorizationRecord(
        id=str(row.id),
        device_code_hash=row.device_code_hash,
        user_code=row.user_code,
        client_name=row.client_name,
        status=DeviceAuthorizationStatus(row.status),
        user_id=str(row.user_id) if row.user_id is not None else None,
        created_at=to_utc(row.created_at),
        updated_at=to_utc(row.updated_at),
        expires_at=to_utc(row.expires_at),
        consumed_at=to_utc_or_none(row.consumed_at),
    )


def user_record_from_table(row: UserTable) -> UserRecord:
    """Map the relational user aggregate without consulting a JSON shadow."""
    return UserRecord(
        id=str(row.id),
        display_name=row.display_name,
        email=row.email,
        avatar_url=row.avatar_url,
        role=PlatformRole(row.role),
        status=UserStatus(row.status),
        created_at=to_utc(row.created_at),
        updated_at=to_utc(row.updated_at),
    )


def user_identity_record_from_table(row: UserIdentityTable) -> UserIdentityRecord:
    return UserIdentityRecord(
        id=str(row.id),
        user_id=str(row.user_id),
        provider=IdentityProvider(row.provider),
        subject=row.subject,
        subject_login=row.subject_login,
        provider_account_created_at=to_utc_or_none(row.provider_account_created_at),
        last_authenticated_at=to_utc_or_none(row.last_authenticated_at),
        created_at=to_utc(row.created_at),
        updated_at=to_utc(row.updated_at),
    )


def workspace_member_record_from_table(row: WorkspaceMemberTable) -> WorkspaceMemberRecord:
    return WorkspaceMemberRecord(
        id=str(row.id),
        workspace_id=str(row.workspace_id),
        user_id=str(row.user_id),
        role=WorkspaceRole(row.role),
        created_at=to_utc(row.created_at),
        updated_at=to_utc(row.updated_at),
    )


def workspace_invitation_record_from_table(
    row: WorkspaceInvitationTable,
) -> WorkspaceInvitationRecord:
    """Map an open offer, leaving the token hash where it is.

    The record is what every caller above this passes around and projects into
    responses, so the secret's digest has no business on it.
    """
    return WorkspaceInvitationRecord(
        id=str(row.id),
        workspace_id=str(row.workspace_id),
        email=row.email,
        role=WorkspaceInvitationRole(row.role),
        invited_by_user_id=_optional_id(row.invited_by_user_id),
        message_id=_optional_id(row.message_id),
        expires_at=to_utc(row.expires_at),
        created_at=to_utc(row.created_at),
        updated_at=to_utc(row.updated_at),
    )


def secret_storage_record_from_table(row: SecretTable) -> SecretStorageRecord:
    """Map encrypted secret columns without consulting flexible payload state."""
    return SecretStorageRecord(
        id=str(row.id),
        workspace_id=str(row.workspace_id),
        name=row.name,
        ciphertext=row.ciphertext,
        created_at=to_utc(row.created_at),
        updated_at=to_utc(row.updated_at),
    )


__all__ = [
    "auth_token_record_from_table",
    "device_authorization_record_from_table",
    "secret_storage_record_from_table",
    "user_identity_record_from_table",
    "user_record_from_table",
    "workspace_invitation_record_from_table",
    "workspace_member_record_from_table",
]
