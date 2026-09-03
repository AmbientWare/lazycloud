from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator

from shared.http.base import HttpModel
from shared.http.workspaces import WorkspaceResponse
from shared.identity import (
    PlatformRole,
    UserStatus,
    WorkspaceInvitationStatus,
    WorkspaceRole,
    normalize_invitation_email,
)


class UserResponse(HttpModel):
    """A person, as the API describes them.

    `github_user_id` is empty for an account with no linked identity. Such an
    account cannot sign in and exists to own tokens.
    """

    id: str
    display_name: str = ""
    email: str = ""
    avatar_url: str = ""
    github_user_id: str = ""
    github_login: str = ""
    role: PlatformRole = PlatformRole.Member
    status: UserStatus = UserStatus.Active
    created_at: datetime
    updated_at: datetime


class UserListResponse(HttpModel):
    data: list[UserResponse] = Field(default_factory=list)
    next: str = ""


class UserCreateRequest(HttpModel):
    """Create an account, optionally pre-linked to the GitHub identity that reaches it.

    Without `github_user_id` the account cannot sign in. With it, that person's
    first sign-in lands here rather than opening a second account for them.
    """

    display_name: str = Field(default="", max_length=255)
    github_user_id: int | None = Field(default=None, gt=0)
    github_login: str = Field(default="", max_length=120)
    role: PlatformRole = PlatformRole.Member


class UserStatusRequest(HttpModel):
    status: UserStatus


class UserRoleRequest(HttpModel):
    role: PlatformRole


class SessionCreateRequest(HttpModel):
    """Redeem the single-use code the GitHub callback handed the browser.

    The code alone is not enough: redemption also requires the sign-in cookie set
    when the flow started, so a code read out of history or a screenshot is spent
    rather than usable.
    """

    code: str = Field(min_length=1, max_length=128)


class SessionResponse(HttpModel):
    """A signed-in session. The token appears here once and is never readable again."""

    token: str
    expires_at: datetime
    user: UserResponse
    return_to: str = ""


class CurrentSessionResponse(HttpModel):
    user: UserResponse
    workspaces: list[WorkspaceResponse] = Field(default_factory=list)


class WorkspaceMemberResponse(HttpModel):
    user_id: str
    display_name: str = ""
    email: str = ""
    role: WorkspaceRole = WorkspaceRole.Member
    created_at: datetime


class WorkspaceMemberListResponse(HttpModel):
    data: list[WorkspaceMemberResponse] = Field(default_factory=list)
    next: str = ""


class WorkspaceMemberAddRequest(HttpModel):
    user_id: str
    role: WorkspaceRole = WorkspaceRole.Member


class WorkspaceMemberRoleRequest(HttpModel):
    role: WorkspaceRole


class WorkspaceInvitationCreateRequest(HttpModel):
    """Invite an address to a workspace. Owner is not a role an invitation can carry."""

    email: str = Field(min_length=3, max_length=320)
    role: WorkspaceRole = WorkspaceRole.Member

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return normalize_invitation_email(value)


class WorkspaceInvitationResponse(HttpModel):
    """An invitation as its workspace's administrators see it."""

    id: str
    workspace_id: str
    email: str
    role: WorkspaceRole = WorkspaceRole.Member
    status: WorkspaceInvitationStatus = WorkspaceInvitationStatus.Pending
    invited_by_user_id: str = ""
    invited_by_name: str = ""
    expires_at: datetime
    created_at: datetime
    updated_at: datetime


class WorkspaceInvitationListResponse(HttpModel):
    data: list[WorkspaceInvitationResponse] = Field(default_factory=list)
    next: str = ""


class PendingInvitationResponse(HttpModel):
    """An invitation as the person it was sent to sees it, once signed in."""

    id: str
    workspace_id: str
    workspace_name: str
    email: str
    role: WorkspaceRole = WorkspaceRole.Member
    invited_by_name: str = ""
    expires_at: datetime
    created_at: datetime


class PendingInvitationListResponse(HttpModel):
    data: list[PendingInvitationResponse] = Field(default_factory=list)
    next: str = ""


__all__ = [
    "CurrentSessionResponse",
    "PendingInvitationListResponse",
    "PendingInvitationResponse",
    "SessionCreateRequest",
    "SessionResponse",
    "UserCreateRequest",
    "UserListResponse",
    "UserResponse",
    "UserRoleRequest",
    "UserStatusRequest",
    "WorkspaceInvitationCreateRequest",
    "WorkspaceInvitationListResponse",
    "WorkspaceInvitationResponse",
    "WorkspaceMemberAddRequest",
    "WorkspaceMemberListResponse",
    "WorkspaceMemberResponse",
    "WorkspaceMemberRoleRequest",
]
