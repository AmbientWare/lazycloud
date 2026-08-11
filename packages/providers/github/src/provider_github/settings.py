from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import ENV_PREFIX

from provider_github.user_identity import GitHubUserIdentity, build_clients

_REQUIRED_VARIABLES = (
    f"{ENV_PREFIX}_GITHUB_CLIENT_ID",
    f"{ENV_PREFIX}_GITHUB_CLIENT_SECRET",
    f"{ENV_PREFIX}_GITHUB_REDIRECT_URI",
)


class GitHubAppSettings(BaseSettings):
    """The GitHub App a person signs in through.

    ``redirect_uri`` is configuration rather than something derived from the incoming
    request. The Host header is set by whoever is calling, so deriving the callback
    from it would let an attacker choose a redirect target GitHub then honours.
    """

    client_id: str = ""
    client_secret: SecretStr = SecretStr("")
    redirect_uri: str = ""
    # Lower than a background integration's: somebody is watching a browser tab.
    timeout_seconds: float = Field(default=10.0, gt=0)

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_GITHUB_",
        extra="ignore",
    )

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret.get_secret_value() and self.redirect_uri)

    def provider(self) -> GitHubUserIdentity:
        """Build the adapter, naming what is missing rather than pretending.

        A deployment without these cannot sign anybody in. Saying so here, at the one
        route that needs it, beats a sign-in button that redirects to a broken page.
        """
        if not self.configured:
            raise ValueError("signing in with GitHub requires " + ", ".join(_REQUIRED_VARIABLES))
        oauth_client, api_client = build_clients(timeout_seconds=self.timeout_seconds)
        return GitHubUserIdentity(
            oauth_client=oauth_client,
            api_client=api_client,
            client_id=self.client_id,
            client_secret=self.client_secret.get_secret_value(),
            redirect_uri=self.redirect_uri,
        )


__all__ = ["GitHubAppSettings"]
