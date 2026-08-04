from __future__ import annotations

from shared.identity import (
    AuthTokenRecord,
    DeviceAuthorizationStatus,
    TokenKind,
    TokenStatus,
)
from shared.timestamps import to_utc, to_utc_or_none

from database.records.identity import DeviceAuthorizationRecord, SecretStorageRecord
from database.tables.identity import DeviceAuthorizationTable, SecretTable, TokenTable


def auth_token_record_from_table(row: TokenTable) -> AuthTokenRecord:
    """Map the relational token aggregate without consulting a JSON shadow."""
    return AuthTokenRecord(
        id=str(row.id),
        name=row.name,
        token_hash=row.token_hash,
        prefix=row.prefix,
        kind=TokenKind(row.kind),
        workspace_id=str(row.workspace_id),
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
        workspace_id=str(row.workspace_id) if row.workspace_id is not None else None,
        created_at=to_utc(row.created_at),
        updated_at=to_utc(row.updated_at),
        expires_at=to_utc(row.expires_at),
        consumed_at=to_utc_or_none(row.consumed_at),
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
]
