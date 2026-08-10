from __future__ import annotations

from shared.identity import (
    AuthTokenRecord,
    DeviceAuthorizationStatus,
    PlatformRole,
    TokenKind,
    TokenStatus,
    UserRecord,
    UserStatus,
    WorkspaceMemberRecord,
    WorkspaceRole,
)
from shared.timestamps import to_utc, to_utc_or_none

from database.records.identity import DeviceAuthorizationRecord, SecretStorageRecord
from database.tables.identity import (
    DeviceAuthorizationTable,
    SecretTable,
    TokenTable,
    UserTable,
    WorkspaceMemberTable,
)


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
        username=row.username,
        password_hash=row.password_hash,
        role=PlatformRole(row.role),
        status=UserStatus(row.status),
        password_changed_at=to_utc(row.password_changed_at),
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
    "user_record_from_table",
    "workspace_member_record_from_table",
]
