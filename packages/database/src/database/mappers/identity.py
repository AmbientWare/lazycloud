from __future__ import annotations

from datetime import UTC, datetime

from shared.identity import (
    AuthTokenRecord,
    DeviceAuthorizationStatus,
    TokenKind,
    TokenStatus,
)

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
        created_at=_utc_datetime(row.created_at),
        last_used_at=_utc_datetime_or_none(row.last_used_at),
        expires_at=_utc_datetime_or_none(row.expires_at),
        revoked_at=_utc_datetime_or_none(row.revoked_at),
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
        created_at=_utc_datetime(row.created_at),
        updated_at=_utc_datetime(row.updated_at),
        expires_at=_utc_datetime(row.expires_at),
        consumed_at=_utc_datetime_or_none(row.consumed_at),
    )


def secret_storage_record_from_table(row: SecretTable) -> SecretStorageRecord:
    """Map encrypted secret columns without consulting flexible payload state."""
    return SecretStorageRecord(
        id=str(row.id),
        workspace_id=str(row.workspace_id),
        name=row.name,
        ciphertext=row.ciphertext,
        created_at=_utc_datetime(row.created_at),
        updated_at=_utc_datetime(row.updated_at),
    )


def _utc_datetime(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _utc_datetime_or_none(value: datetime | None) -> datetime | None:
    return _utc_datetime(value) if value is not None else None


__all__ = [
    "auth_token_record_from_table",
    "device_authorization_record_from_table",
    "secret_storage_record_from_table",
]
