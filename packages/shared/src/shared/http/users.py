from __future__ import annotations

from datetime import datetime

from pydantic import Field, SecretStr

from shared.http.base import HttpModel
from shared.http.workspaces import WorkspaceResponse
from shared.identity import PlatformRole, UserStatus, WorkspaceRole


class UserResponse(HttpModel):
    """A person, as the API describes them. Never carries the password hash."""

    id: str
    username: str
    role: PlatformRole = PlatformRole.Member
    status: UserStatus = UserStatus.Active
    created_at: datetime
    updated_at: datetime


class UserListResponse(HttpModel):
    data: list[UserResponse] = Field(default_factory=list)
    next: str = ""


class UserCreateRequest(HttpModel):
    username: str = Field(min_length=3, max_length=64)
    password: SecretStr
    role: PlatformRole = PlatformRole.Member


class PasswordChangeRequest(HttpModel):
    current_password: SecretStr | None = None
    """Required when changing your own password; omitted for an administrator reset."""

    new_password: SecretStr


class UserStatusRequest(HttpModel):
    status: UserStatus


class SessionCreateRequest(HttpModel):
    username: str
    password: SecretStr


class SessionResponse(HttpModel):
    """A signed-in session. The token appears here once and is never readable again."""

    token: str
    expires_at: datetime
    user: UserResponse


class CurrentSessionResponse(HttpModel):
    user: UserResponse
    workspaces: list[WorkspaceResponse] = Field(default_factory=list)


class WorkspaceMemberResponse(HttpModel):
    user_id: str
    username: str
    role: WorkspaceRole = WorkspaceRole.Member
    created_at: datetime


class WorkspaceMemberListResponse(HttpModel):
    data: list[WorkspaceMemberResponse] = Field(default_factory=list)
    next: str = ""


class WorkspaceMemberAddRequest(HttpModel):
    username: str
    role: WorkspaceRole = WorkspaceRole.Member


class WorkspaceMemberRoleRequest(HttpModel):
    role: WorkspaceRole


__all__ = [
    "CurrentSessionResponse",
    "PasswordChangeRequest",
    "SessionCreateRequest",
    "SessionResponse",
    "UserCreateRequest",
    "UserListResponse",
    "UserResponse",
    "UserStatusRequest",
    "WorkspaceMemberAddRequest",
    "WorkspaceMemberListResponse",
    "WorkspaceMemberResponse",
    "WorkspaceMemberRoleRequest",
]
