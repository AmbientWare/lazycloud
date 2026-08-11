from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import Field

from shared.contracts import ContractModel
from shared.identity import IdentityProvider


class ExternalIdentityProfile(ContractModel):
    """Who the provider says this person is, at the moment they signed in.

    ``subject`` is the provider's own immutable identifier and is the only field
    an account may be keyed on. ``login`` and ``email`` are both reassignable —
    someone else can end up holding either one — so they describe the person
    rather than identify them.
    """

    provider: IdentityProvider = IdentityProvider.Github
    subject: str = Field(min_length=1, max_length=64)
    login: str = Field(default="", max_length=120)
    display_name: str = Field(default="", max_length=255)
    email: str = Field(default="", max_length=320)
    avatar_url: str = Field(default="", max_length=1024)
    account_created_at: datetime | None = None


class ExternalIdentityProvider(Protocol):
    """The authorization-code half of an external sign-in."""

    def authorize_url(self, *, state: str, code_challenge: str) -> str:
        """Where to send the browser to ask this person to authorize us."""
        ...

    def identify(self, *, code: str, code_verifier: str) -> ExternalIdentityProfile:
        """Redeem an authorization code and return who it belongs to.

        The provider access token exists only inside this call. Returning it would
        make every caller upstream a place it could be logged, stored, or forwarded,
        and nothing above this boundary has any use for it.
        """
        ...


__all__ = ["ExternalIdentityProfile", "ExternalIdentityProvider"]
