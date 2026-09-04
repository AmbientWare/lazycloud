from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from identity.invitations import InvitationListing, PendingInvitation
from pydantic import JsonValue
from shared.http.users import (
    PendingInvitationListResponse,
    PendingInvitationResponse,
    WorkspaceInvitationCreateRequest,
    WorkspaceInvitationListResponse,
    WorkspaceInvitationResponse,
    WorkspaceMemberResponse,
)
from shared.identity import AuthScope, WorkspaceInvitationRecord, WorkspaceRole

from api.server.auth import read_principal, read_token, write_principal, write_token
from api.server.dependencies import (
    authorize_token_workspace,
    current_services,
    require_user_principal,
)
from api.server.identifiers import resource_identifier
from api.server.routers.control_plane.users import member_response
from api.server.services import ApiServices
from billing import DatabaseBillingAdmission

router = APIRouter()


def _invitation_response(listing: InvitationListing) -> WorkspaceInvitationResponse:
    return WorkspaceInvitationResponse.model_validate(
        _invitation_fields(listing.invitation, _ADMINISTRATOR_FIELDS)
        | {"invited_by_name": listing.invited_by_name}
    )


def _pending_response(pending: PendingInvitation) -> PendingInvitationResponse:
    """The invitee's view, which names the workspace and says nothing of who else was asked."""
    return PendingInvitationResponse.model_validate(
        _invitation_fields(pending.invitation, _INVITEE_FIELDS)
        | {
            "workspace_name": pending.workspace.name,
            "invited_by_name": pending.invited_by_name,
        }
    )


# What leaves the API, stated once per audience rather than as two hand-copied
# argument lists that a new field is silently missing from.
_ADMINISTRATOR_FIELDS = {
    "id",
    "workspace_id",
    "email",
    "role",
    "status",
    "invited_by_user_id",
    "expires_at",
    "created_at",
    "updated_at",
}
_INVITEE_FIELDS = {"id", "workspace_id", "email", "role", "expires_at", "created_at"}


def _invitation_fields(
    invitation: WorkspaceInvitationRecord,
    fields: set[str],
) -> dict[str, JsonValue]:
    return invitation.model_dump(mode="json", include=fields)


@router.get(
    "/api/v1/workspaces/{workspace}/invitations",
    response_model=WorkspaceInvitationListResponse,
    operation_id="list_workspace_invitations",
)
def list_workspace_invitations(
    workspace: str,
    principal: read_principal,
    services: ApiServices = Depends(current_services),
) -> WorkspaceInvitationListResponse:
    """Who has been asked in but has not answered.

    An administrator's view rather than a member's: these are the addresses of
    people with no membership here, and a member who can read them learns who the
    workspace is recruiting.
    """
    workspace_id = authorize_token_workspace(
        services,
        principal.token,
        workspace,
        AuthScope.Read,
        platform_role=principal.platform_role,
        required_role=WorkspaceRole.Administrator,
    )
    return WorkspaceInvitationListResponse(
        data=[_invitation_response(item) for item in services.invitations.pending(workspace_id)]
    )


@router.post(
    "/api/v1/workspaces/{workspace}/invitations",
    response_model=WorkspaceInvitationResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_workspace_invitation",
)
def create_workspace_invitation(
    workspace: str,
    request: WorkspaceInvitationCreateRequest,
    principal: write_principal,
    services: ApiServices = Depends(current_services),
) -> WorkspaceInvitationResponse:
    """Inviting someone is an administrator's decision, like adding them outright."""
    workspace_id = authorize_token_workspace(
        services,
        principal.token,
        workspace,
        AuthScope.Write,
        platform_role=principal.platform_role,
        required_role=WorkspaceRole.Administrator,
    )
    listing = services.invitations.invite(
        workspace_id,
        email=request.email,
        role=request.role,
        actor=principal.token,
        admission=DatabaseBillingAdmission(),
    )
    return _invitation_response(listing)


@router.post(
    "/api/v1/workspaces/{workspace}/invitations/{invitation_id}/resend",
    response_model=WorkspaceInvitationResponse,
    operation_id="resend_workspace_invitation",
)
def resend_workspace_invitation(
    workspace: str,
    invitation_id: str,
    principal: write_principal,
    services: ApiServices = Depends(current_services),
) -> WorkspaceInvitationResponse:
    workspace_id = authorize_token_workspace(
        services,
        principal.token,
        workspace,
        AuthScope.Write,
        platform_role=principal.platform_role,
        required_role=WorkspaceRole.Administrator,
    )
    listing = services.invitations.resend(
        workspace_id,
        resource_identifier(invitation_id, resource="invitation"),
        actor=principal.token,
    )
    return _invitation_response(listing)


@router.delete(
    "/api/v1/workspaces/{workspace}/invitations/{invitation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="revoke_workspace_invitation",
)
def revoke_workspace_invitation(
    workspace: str,
    invitation_id: str,
    principal: write_principal,
    services: ApiServices = Depends(current_services),
) -> Response:
    workspace_id = authorize_token_workspace(
        services,
        principal.token,
        workspace,
        AuthScope.Write,
        platform_role=principal.platform_role,
        required_role=WorkspaceRole.Administrator,
    )
    services.invitations.revoke(
        workspace_id,
        resource_identifier(invitation_id, resource="invitation"),
        actor=principal.token,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/api/v1/invitations",
    response_model=PendingInvitationListResponse,
    operation_id="list_pending_invitations",
)
def list_pending_invitations(
    token: read_token,
    services: ApiServices = Depends(current_services),
) -> PendingInvitationListResponse:
    """What is waiting for the signed-in person, by the address they signed in with."""
    user_id = require_user_principal(token)
    return PendingInvitationListResponse(
        data=[_pending_response(item) for item in services.invitations.pending_for_user(user_id)]
    )


@router.post(
    "/api/v1/invitations/{invitation_id}/accept",
    response_model=WorkspaceMemberResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="accept_invitation",
)
def accept_invitation(
    invitation_id: str,
    token: write_token,
    services: ApiServices = Depends(current_services),
) -> WorkspaceMemberResponse:
    require_user_principal(token)
    accepted = services.invitations.accept(
        resource_identifier(invitation_id, resource="invitation"),
        actor=token,
        admission=DatabaseBillingAdmission(),
    )
    return member_response(accepted.user, accepted.membership)


@router.post(
    "/api/v1/invitations/{invitation_id}/decline",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="decline_invitation",
)
def decline_invitation(
    invitation_id: str,
    token: write_token,
    services: ApiServices = Depends(current_services),
) -> Response:
    require_user_principal(token)
    services.invitations.decline(
        resource_identifier(invitation_id, resource="invitation"),
        actor=token,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router"]
