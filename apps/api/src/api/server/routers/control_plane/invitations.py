from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from identity.invitations import InvitationListing, InvitationPreview
from shared.http.users import (
    InvitationPreviewResponse,
    WorkspaceInvitationCreateRequest,
    WorkspaceInvitationListResponse,
    WorkspaceInvitationResponse,
    WorkspaceMemberResponse,
)
from shared.identity import AuthScope, WorkspaceRole

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
    invitation = listing.invitation
    return WorkspaceInvitationResponse(
        id=invitation.id,
        workspace_id=invitation.workspace_id,
        email=invitation.email,
        role=invitation.role,
        invited_by_user_id=invitation.invited_by_user_id,
        invited_by_name=listing.invited_by_name,
        expired=listing.expired,
        expires_at=invitation.expires_at,
        created_at=invitation.created_at,
        updated_at=invitation.updated_at,
    )


def _preview_response(preview: InvitationPreview) -> InvitationPreviewResponse:
    return InvitationPreviewResponse(
        workspace_id=preview.workspace.id,
        workspace_name=preview.workspace.name,
        email=preview.invitation.email,
        role=preview.invitation.role,
        invited_by_name=preview.invited_by_name,
        expired=preview.expired,
        expires_at=preview.invitation.expires_at,
    )


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
    people with no membership here, and a member who could read them would learn
    who the workspace is recruiting.
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
        data=[_invitation_response(item) for item in services.invitations.open_offers(workspace_id)]
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
    """Inviting someone is an administrator's decision, like adding them outright.

    Returns once the offer and its message are committed. Delivery happens on the
    drain, so this does not wait on the email provider and a provider outage
    delays the message rather than refusing the invitation.
    """
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
    """Send the offer again on a new link. The previous link stops working."""
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
    "/api/v1/invitations/{token}",
    response_model=InvitationPreviewResponse,
    operation_id="preview_invitation",
)
def preview_invitation(
    token: str,
    credential: read_token,
    services: ApiServices = Depends(current_services),
) -> InvitationPreviewResponse:
    """What the link opens onto, so somebody sees what they are joining first.

    Reading does not redeem: a mail scanner or a link preview follows this and
    must not spend the offer. Signed in, because accepting will be, and bouncing
    somebody to sign-in at the preview rather than at the button is one round
    trip fewer through a flow that already leaves the site.
    """
    require_user_principal(credential)
    return _preview_response(services.invitations.preview(token))


@router.post(
    "/api/v1/invitations/{token}/accept",
    response_model=WorkspaceMemberResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="accept_invitation",
)
def accept_invitation(
    token: str,
    credential: write_token,
    services: ApiServices = Depends(current_services),
) -> WorkspaceMemberResponse:
    """Redeem the link as whoever is signed in, and join.

    A POST rather than a GET on the link itself, so nothing that merely follows
    a URL, a scanner or a prefetch, can join a workspace on somebody's behalf.
    """
    require_user_principal(credential)
    accepted = services.invitations.accept(
        token,
        actor=credential,
        admission=DatabaseBillingAdmission(),
    )
    return member_response(accepted.user, accepted.membership)


@router.post(
    "/api/v1/invitations/{token}/decline",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="decline_invitation",
)
def decline_invitation(
    token: str,
    credential: write_token,
    services: ApiServices = Depends(current_services),
) -> Response:
    require_user_principal(credential)
    services.invitations.decline(token, actor=credential)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router"]
