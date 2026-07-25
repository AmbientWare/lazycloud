from __future__ import annotations

from datetime import datetime

from shared.http.base import HttpModel
from shared.identity import DeviceAuthorizationStatus

DEVICE_AUTHORIZATION_VERIFICATION_PATH = "activate"
"""Web route (relative to the control-plane base URL) where a signed-in user
approves a pending device-code login."""


class DeviceCodeCreateRequest(HttpModel):
    client_name: str = "cli"


class DeviceCodeCreateResponse(HttpModel):
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in_seconds: int
    poll_interval_seconds: int


class DeviceCodeTokenRequest(HttpModel):
    device_code: str


class DeviceCodeTokenResponse(HttpModel):
    """Poll outcome for a device-code login.

    ``status`` is a domain outcome, not an error envelope: pending polls
    return 200 with ``pending`` until the user approves, denies, or the code
    expires. ``token`` and ``workspace`` are only set once on approval.
    """

    status: DeviceAuthorizationStatus
    token: str = ""
    workspace: str = ""


class DeviceCodeResponse(HttpModel):
    user_code: str
    client_name: str
    status: DeviceAuthorizationStatus
    created_at: datetime
    expires_at: datetime


class DeviceCodeApproveRequest(HttpModel):
    workspace: str


__all__ = [
    "DEVICE_AUTHORIZATION_VERIFICATION_PATH",
    "DeviceCodeApproveRequest",
    "DeviceCodeCreateRequest",
    "DeviceCodeCreateResponse",
    "DeviceCodeResponse",
    "DeviceCodeTokenRequest",
    "DeviceCodeTokenResponse",
]
