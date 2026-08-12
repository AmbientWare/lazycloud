from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import ENV_PREFIX

from provider_stripe.billing import StripeBilling, build_client


class StripeSettings(BaseSettings):
    """The credential that moves money.

    A `SecretStr` because settings objects reach logs and error reports, and a
    key that appears in one is a key that has to be rotated.
    """

    api_key: SecretStr = SecretStr("")
    webhook_secret: SecretStr = SecretStr("")
    """The endpoint secret deliveries are signed with — a different secret from
    the API key, issued per endpoint, and the only thing standing between a
    public URL and anonymous changes to what customers owe."""

    timeout_seconds: float = Field(default=30.0, gt=0)

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_STRIPE_",
        extra="ignore",
    )

    @property
    def configured(self) -> bool:
        return bool(self.api_key.get_secret_value())

    @property
    def webhooks_configured(self) -> bool:
        """Separate from `configured` because the two arrive separately.

        A deployment can issue invoices before its endpoint is registered — the
        secret only exists once the endpoint does — and one that can charge but
        cannot yet hear the outcome is a real intermediate state, not a
        misconfiguration.
        """

        return bool(self.webhook_secret.get_secret_value())

    def provider(self) -> StripeBilling:
        """Build the adapter, refusing to pretend when nothing was configured.

        Named rather than silently returning a no-op: a deployment missing this
        cannot bill anyone, and saying so once here beats discovering it as usage
        that metered correctly and was never charged.
        """

        if not self.configured:
            raise ValueError(f"billing requires {ENV_PREFIX}_STRIPE_API_KEY")
        return StripeBilling(
            client=build_client(
                api_key=self.api_key.get_secret_value(),
                timeout_seconds=self.timeout_seconds,
            )
        )


__all__ = ["StripeSettings"]
