from __future__ import annotations

from collections.abc import Callable

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import ENV_PREFIX
from shared.deployment_settings import MissingDeploymentSettingError

from provider_resend.client import ResendEmailSender, build_client


class ResendSettings(BaseSettings):
    """The credential and sender address transactional email goes out under.

    A `SecretStr` because settings objects reach logs and error reports, and a
    key that appears in one is a key that has to be rotated.
    """

    api_key: SecretStr = SecretStr("")
    from_address: str = "LazyCloud <noreply@lazycloud.dev>"
    """Must be on a domain the Resend account has verified, or every send is
    refused with a 403 naming the domain."""

    timeout_seconds: float = Field(default=15.0, gt=0)

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_RESEND_",
        extra="ignore",
    )

    @property
    def configured(self) -> bool:
        return bool(self.api_key.get_secret_value())

    def sender(self) -> ResendEmailSender:
        """Build the adapter, refusing to pretend when nothing was configured.

        A deployment missing this cannot invite anybody by email, and saying so
        here, naming the variable, beats an invitation the dashboard reports as
        sent and nobody receives.
        """
        if not self.configured:
            raise MissingDeploymentSettingError(
                f"{ENV_PREFIX}_RESEND_API_KEY",
                purpose="the email provider this deployment sends invitations through",
            )
        return ResendEmailSender(
            client=build_client(
                api_key=self.api_key.get_secret_value(),
                timeout_seconds=self.timeout_seconds,
            ),
            from_address=self.from_address,
        )

    def sender_factory(self) -> Callable[[], ResendEmailSender]:
        """An adapter built at most once, and never before something asks for it.

        Lazy so a deployment without the credential still starts and fails at the
        invite route naming what is missing. Not cached on failure: the next
        attempt raises again rather than serving an adapter that was never built.
        """
        built: list[ResendEmailSender] = []

        def sender() -> ResendEmailSender:
            if not built:
                built.append(self.sender())
            return built[0]

        return sender


__all__ = ["ResendSettings"]
