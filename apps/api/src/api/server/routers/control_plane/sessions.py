from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from shared.http.users import (
    CurrentSessionResponse,
    SessionCreateRequest,
    SessionResponse,
)
from shared.http.workspaces import workspace_response

from api.server.auth import read_token
from api.server.dependencies import current_services, require_user_principal
from api.server.routers.control_plane.users import user_response
from api.server.services import ApiServices

router = APIRouter()


@router.post(
    "/api/v1/sessions",
    response_model=SessionResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="sign_in",
)
def sign_in(
    request: SessionCreateRequest,
    services: ApiServices = Depends(current_services),
) -> SessionResponse:
    """Exchange a username and password for a session credential.

    Guessing budget is enforced by the unauthenticated rate-limit middleware, which
    caps this prefix per address and globally—the global cap is what bounds a
    credential-stuffing run spread across many addresses.
    """
    session = services.sessions.sign_in(
        username=request.username,
        password=request.password.get_secret_value(),
    )
    return SessionResponse(
        token=session.token,
        expires_at=session.expires_at,
        user=user_response(session.user),
    )


@router.get(
    "/api/v1/sessions/current",
    response_model=CurrentSessionResponse,
    operation_id="get_current_session",
)
def get_current_session(
    token: read_token,
    services: ApiServices = Depends(current_services),
) -> CurrentSessionResponse:
    user_id = require_user_principal(services, token)
    user = services.users.get(user_id)
    return CurrentSessionResponse(
        user=user_response(user),
        workspaces=[workspace_response(record) for record in services.users.workspaces(user.id)],
    )


@router.delete(
    "/api/v1/sessions/current",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="sign_out",
)
def sign_out(
    token: read_token,
    services: ApiServices = Depends(current_services),
) -> Response:
    """Revoke the credential that made this request, and nothing else.

    Signing out in one place must not sign the person out everywhere, so this ends
    the presented session rather than every session the account holds.
    """
    require_user_principal(services, token)
    services.auth.revoke_token(token.id)
    services.auth.credentials_revoked()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router"]
