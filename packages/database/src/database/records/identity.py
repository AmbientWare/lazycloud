from __future__ import annotations

from datetime import datetime

from pydantic import Field
from shared.contracts import ContractModel
from shared.http.workspaces import WorkspaceAuditAction, WorkspaceAuditTarget
from shared.identity import DeviceAuthorizationStatus
from shared.timestamps import utc_now


class DeviceAuthorizationRecord(ContractModel):
    """One pending or decided short-lived device-code login request."""

    id: str
    device_code_hash: str
    user_code: str
    client_name: str = "cli"
    status: DeviceAuthorizationStatus = DeviceAuthorizationStatus.Pending
    user_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime
    consumed_at: datetime | None = None


class SecretStorageRecord(ContractModel):
    """Encrypted workspace-secret persistence record."""

    id: str
    workspace_id: str
    name: str
    ciphertext: str = Field(repr=False)
    created_at: datetime
    updated_at: datetime


class WorkspaceAuditRecord(ContractModel):
    id: str
    workspace_id: str
    action: WorkspaceAuditAction
    actor_token_id: str | None = None
    actor_user_id: str | None = None
    """Account behind the change, kept because a token can be revoked and a person cannot."""

    actor_name: str
    target_type: WorkspaceAuditTarget
    target_id: str
    target_name: str
    summary: str
    previous_value: str | None = None
    new_value: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


__all__ = ["DeviceAuthorizationRecord", "SecretStorageRecord", "WorkspaceAuditRecord"]
