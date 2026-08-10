from __future__ import annotations

from datetime import datetime

from pydantic import Field
from shared.contracts import ContractModel
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


__all__ = ["DeviceAuthorizationRecord", "SecretStorageRecord"]
