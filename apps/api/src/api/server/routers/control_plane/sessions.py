from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import RedirectResponse
from identity.auth import AuthError
from identity.sign_in import SIGN_IN_STATE_TTL_SECONDS, SignInStateStoreError
from shared.deployment_settings import MissingDeploymentSettingError
from shared.errors import InvalidInputError
from shared.http.users import (
    CurrentSessionResponse,
    SessionCreateRequest,
    SessionResponse,
)
from shared.http.workspaces import workspace_response
from shared.urls import normalize_return_path

from api.server.auth import read_token
from api.server.dependencies import current_services, require_user_principal
from api.server.routers.control_plane.users import user_response
from api.server.services import ApiServices

LOGGER = logging.getLogger(__name__)

router = APIRouter()

SIGN_IN_NONCE_COOKIE = "lazycloud_sign_in"
_SIGN_IN_LANDING_PATH = "/signin"
_SIGN_IN_COMPLETE_PATH = "/callback"


@router.get(
    "/auth/github/start",
    operation_id="start_github_sign_in",
    response_class=RedirectResponse,
    status_code=status.HTTP_302_FOUND,
)
def start_github_sign_in(
    return_to: str = "",
    services: ApiServices = Depends(current_services),
) -> RedirectResponse:
    """Send the browser to GitHub, remembering where to put them afterwards.

    A plain link rather than a scripted POST, so the sign-in control is an anchor
    that behaves like one. `return_to` is kept server-side against the state secret
    instead of travelling through GitHub, which is what lets `/activate?code=...`
    survive the round trip without becoming a redirect target anyone can set.
    """
    try:
        destination = normalize_return_path(return_to)
    except ValueError:
        return _failed(_SIGN_IN_LANDING_PATH, "invalid_return_to")
    try:
        start = services.sign_in.start(return_to=destination)
    except ValueError as exc:
        # No GitHub App configured. Loud, and naming the variables, because the
        # alternative is a sign-in button that leads somewhere broken.
        LOGGER.error("github sign-in is not configured: %s", exc)
        return _failed(_SIGN_IN_LANDING_PATH, "provider_unavailable")
    except SignInStateStoreError:
        LOGGER.exception("sign-in state storage is unavailable")
        return _failed(_SIGN_IN_LANDING_PATH, "provider_unavailable")
    redirect = RedirectResponse(start.authorize_url, status_code=status.HTTP_302_FOUND)
    redirect.set_cookie(
        SIGN_IN_NONCE_COOKIE,
        start.nonce,
        max_age=SIGN_IN_STATE_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        # Unconditional rather than derived from the scheme. Browsers treat
        # http://localhost as a secure context and send Secure cookies to it, so
        # local development needs no exception and production gets no weaker cookie.
        secure=True,
        path="/",
    )
    return redirect


@router.get(
    "/auth/github/callback",
    operation_id="complete_github_sign_in",
    response_class=RedirectResponse,
    status_code=status.HTTP_303_SEE_OTHER,
)
def complete_github_sign_in(
    code: str = "",
    state: str = "",
    error: str = "",
    services: ApiServices = Depends(current_services),
) -> RedirectResponse:
    """Land the browser back here with a single-use code it can trade for a session.

    This is the one route that answers a failure with a redirect rather than an
    `ErrorResponse`. The caller is a browser navigating, and a JSON body rendered as
    a bare document is a dead end for the person reading it. The reason is drawn from
    a closed set and GitHub's own error text is never reflected back into our page.

    Success is a fragment, not a query: `#code=` is never sent to any server, so the
    exchange code stays out of access logs and `Referer` headers. It is also useless
    without the sign-in cookie, so it is not a credential on its own.
    """
    if error:
        LOGGER.info("github sign-in was refused upstream")
        return _failed(_SIGN_IN_LANDING_PATH, "access_denied")
    if not code or not state:
        return _failed(_SIGN_IN_LANDING_PATH, "invalid_state")
    try:
        exchange_code = services.sign_in.complete(code=code, state=state)
    except AuthError:
        LOGGER.info("github sign-in state was expired, replayed, or unknown")
        return _failed(_SIGN_IN_LANDING_PATH, "invalid_state")
    except MissingDeploymentSettingError as exc:
        LOGGER.error("sign-in cannot complete, a required integration is not configured: %s", exc)
        return _failed(_SIGN_IN_LANDING_PATH, "provider_unavailable")
    except SignInStateStoreError:
        LOGGER.exception("sign-in state storage is unavailable")
        return _failed(_SIGN_IN_LANDING_PATH, "provider_unavailable")
    except InvalidInputError:
        # Refused rather than failed: an integration rejected something about
        # this account, and the same attempt made again is refused again. Telling
        # the person to come back shortly would be telling them to keep doing the
        # one thing that cannot work.
        LOGGER.exception("sign-in was refused by an integration and will be refused again")
        return _failed(_SIGN_IN_LANDING_PATH, "provider_refused")
    except Exception:
        # This route answers a browser mid-navigation, so every failure has to
        # land on the sign-in page. Anything reaching here uncaught would
        # otherwise be rendered to the person as a bare JSON document — including
        # the ones provisioning raises, which are not domain errors.
        LOGGER.exception("sign-in could not be completed")
        return _failed(_SIGN_IN_LANDING_PATH, "provider_unavailable")
    return RedirectResponse(
        f"{_SIGN_IN_COMPLETE_PATH}#code={exchange_code}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post(
    "/api/v1/sessions",
    response_model=SessionResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="sign_in",
)
def sign_in(
    request: Request,
    payload: SessionCreateRequest,
    response: Response,
    services: ApiServices = Depends(current_services),
) -> SessionResponse:
    """Redeem the single-use sign-in code for the session credential.

    Redemption needs the cookie set when the flow started as well as the code, so a
    code lifted from browser history or a shoulder-surfed screen is not enough.
    """
    session = services.sign_in.redeem(
        code=payload.code,
        nonce=request.cookies.get(SIGN_IN_NONCE_COOKIE, ""),
    )
    response.delete_cookie(SIGN_IN_NONCE_COOKIE, path="/")
    return SessionResponse(
        token=session.token,
        expires_at=session.expires_at,
        user=user_response(session.user, services.users.identity(session.user.id)),
        return_to=session.return_to,
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
    user_id = require_user_principal(token)
    user = services.users.get(user_id)
    return CurrentSessionResponse(
        user=user_response(user, services.users.identity(user.id)),
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
    require_user_principal(token)
    services.auth.revoke_token(token.id)
    services.auth.credentials_revoked()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _failed(path: str, reason: str) -> RedirectResponse:
    return RedirectResponse(f"{path}?error={reason}", status_code=status.HTTP_303_SEE_OTHER)


__all__ = ["SIGN_IN_NONCE_COOKIE", "router"]
