from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from shared.errors import InvalidInputError, UpstreamUnavailableError
from shared.external_identity import ExternalIdentityProfile
from shared.identity import IdentityProvider

GITHUB_OAUTH_BASE_URL = "https://github.com"
GITHUB_API_BASE_URL = "https://api.github.com"

_API_VERSION = "2022-11-28"
_ACCEPT_GITHUB_JSON = "application/vnd.github+json"


class _GitHubPayload(BaseModel):
    """GitHub sends far more than we read, so unknown fields are expected here."""

    model_config = ConfigDict(extra="ignore")


class _TokenPayload(_GitHubPayload):
    access_token: str = Field(default="", repr=False)
    error: str = ""
    error_description: str = ""


class _AccountPayload(_GitHubPayload):
    # The one field an account cannot be resolved without. GitHub always sends it,
    # and a payload without it is not something to guess at.
    id: int
    login: str = ""
    name: str | None = None
    avatar_url: str = ""
    created_at: datetime | None = None


class _EmailPayload(_GitHubPayload):
    email: str = ""
    primary: bool = False
    verified: bool = False


class _ApiErrorPayload(_GitHubPayload):
    message: str = ""


_EMAILS_ADAPTER: TypeAdapter[list[_EmailPayload]] = TypeAdapter(list[_EmailPayload])


@dataclass(frozen=True, slots=True)
class GitHubUserIdentity:
    """A GitHub App's user authorization flow, used only to learn who someone is.

    Two clients because the hosts differ and only one of them takes a bearer token:
    a single client carrying an ``Authorization`` header would send the user's access
    token back to the endpoint that issued it.
    """

    oauth_client: httpx.Client
    api_client: httpx.Client
    client_id: str
    client_secret: str = field(repr=False)
    redirect_uri: str

    def authorize_url(self, *, state: str, code_challenge: str) -> str:
        # No `scope`: a GitHub App's permissions are configured on the App itself, and
        # sending one would be an OAuth-App parameter that means nothing here.
        query = urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{GITHUB_OAUTH_BASE_URL}/login/oauth/authorize?{query}"

    def identify(self, *, code: str, code_verifier: str) -> ExternalIdentityProfile:
        access_token = self._exchange_code(code=code, code_verifier=code_verifier)
        account = self._account(access_token)
        return ExternalIdentityProfile(
            provider=IdentityProvider.Github,
            subject=str(account.id),
            login=account.login,
            # Normalized here rather than in the browser: a person who set no display
            # name still has to render as something, and one answer beats every
            # consumer inventing its own.
            display_name=account.name or account.login,
            email=self._primary_verified_email(access_token),
            avatar_url=account.avatar_url,
            account_created_at=account.created_at,
        )

    def _exchange_code(self, *, code: str, code_verifier: str) -> str:
        """Trade the authorization code for a token used once and then dropped."""
        response = self._post(
            "/login/oauth/access_token",
            {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code": code,
                "redirect_uri": self.redirect_uri,
                "code_verifier": code_verifier,
            },
        )
        payload = _validated(response, _TokenPayload)
        # GitHub answers a rejected code with HTTP 200 and an `error` field, so the
        # status alone does not say whether this worked.
        if payload.error:
            raise InvalidInputError(_error_detail(payload))
        _raise_for_status(response)
        if not payload.access_token:
            raise UpstreamUnavailableError("GitHub returned no access token")
        return payload.access_token

    def _account(self, access_token: str) -> _AccountPayload:
        response = self._get("/user", access_token)
        _raise_for_status(response)
        return _validated(response, _AccountPayload)

    def _primary_verified_email(self, access_token: str) -> str:
        response = self._get("/user/emails", access_token)
        if httpx.codes.BAD_REQUEST <= response.status_code < httpx.codes.INTERNAL_SERVER_ERROR:
            # The App is missing the "Email addresses (read-only)" user permission.
            # Reading that as "this person has no email" would bury a deployment
            # mistake in every account that ever signs in, where nothing surfaces it.
            raise UpstreamUnavailableError(
                "GitHub refused the account's email addresses; the GitHub App is missing "
                "the Email addresses (read-only) user permission"
            )
        _raise_for_status(response)
        try:
            entries = _EMAILS_ADAPTER.validate_json(response.content)
        except ValidationError as exc:
            raise UpstreamUnavailableError("GitHub returned an unreadable email list") from exc
        for entry in entries:
            if entry.primary and entry.verified:
                return entry.email
        # A real per-person state rather than a failure: an account can have no
        # verified primary address, and that must not stop them signing in.
        return ""

    def _post(self, path: str, data: dict[str, str]) -> httpx.Response:
        try:
            # Without an explicit Accept, GitHub answers this endpoint form-encoded.
            return self.oauth_client.post(
                path,
                headers={"Accept": "application/json"},
                data=data,
            )
        except httpx.RequestError as exc:
            raise UpstreamUnavailableError(f"GitHub request failed: {exc!s}") from exc

    def _get(self, path: str, access_token: str) -> httpx.Response:
        try:
            return self.api_client.get(
                path,
                headers={
                    "Accept": _ACCEPT_GITHUB_JSON,
                    "Authorization": f"Bearer {access_token}",
                    "X-GitHub-Api-Version": _API_VERSION,
                },
            )
        except httpx.RequestError as exc:
            raise UpstreamUnavailableError(f"GitHub request failed: {exc!s}") from exc


def _validated[TPayload: _GitHubPayload](
    response: httpx.Response,
    payload_type: type[TPayload],
) -> TPayload:
    try:
        return payload_type.model_validate_json(response.content)
    except ValidationError as exc:
        raise UpstreamUnavailableError(
            f"GitHub returned an unreadable response ({response.status_code})"
        ) from exc


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code < httpx.codes.BAD_REQUEST:
        return
    message = f"GitHub rejected the request ({response.status_code})"
    try:
        detail = _ApiErrorPayload.model_validate_json(response.content).message
    except ValidationError:
        detail = ""
    if detail:
        message = f"{message}: {detail}"
    # A refused request is something about this caller or this App; a failed one is
    # GitHub's, and only the second is worth trying again.
    if response.status_code < httpx.codes.INTERNAL_SERVER_ERROR:
        raise InvalidInputError(message)
    raise UpstreamUnavailableError(message)


def _error_detail(payload: _TokenPayload) -> str:
    # `error_uri` is deliberately never read: it is a link GitHub chose, and
    # reflecting a provider-supplied URL into our own error puts it in front of
    # a person.
    return (
        f"GitHub rejected the authorization: {payload.error}: {payload.error_description}"
    ).rstrip(": ")


def build_clients(*, timeout_seconds: float = 10.0) -> tuple[httpx.Client, httpx.Client]:
    """The OAuth host and the API host, in that order."""
    return (
        httpx.Client(base_url=GITHUB_OAUTH_BASE_URL, timeout=timeout_seconds),
        httpx.Client(base_url=GITHUB_API_BASE_URL, timeout=timeout_seconds),
    )


__all__ = [
    "GITHUB_API_BASE_URL",
    "GITHUB_OAUTH_BASE_URL",
    "GitHubUserIdentity",
    "build_clients",
]
