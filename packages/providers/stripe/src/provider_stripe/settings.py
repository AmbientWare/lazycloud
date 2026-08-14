from __future__ import annotations

from collections.abc import Callable

import httpx
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import ENV_PREFIX
from shared.deployment_settings import MissingDeploymentSettingError

from provider_stripe.api import build_client
from provider_stripe.billing import StripeBilling
from provider_stripe.catalog import StripeCatalog


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

        return StripeBilling(client=self._client())

    def provider_factory(self) -> Callable[[], StripeBilling]:
        """An adapter built at most once, and never before something asks for it.

        Lazy because a deployment missing the credential has to fail where the
        work is, naming the variable on every attempt, rather than once in a boot
        log nobody reads back. Built once because a caller that rebuilt per call
        would open a connection pool per call and close none of them.

        Not cached on failure: the next attempt raises again rather than serving
        an adapter that was never built.
        """

        built: list[StripeBilling] = []

        def provider() -> StripeBilling:
            if not built:
                built.append(self.provider())
            return built[0]

        return provider

    def catalog(self) -> StripeCatalog:
        """Build the operator's view of the plan, the meters and the prices.

        Separate from `provider()` because the two are used by different people
        at different times: the catalog is published once by an operator holding
        a key for one named account, and the payment relationship is worked by
        every process that bills.
        """

        return StripeCatalog(client=self._client())

    def _client(self) -> httpx.Client:
        if not self.configured:
            raise MissingDeploymentSettingError(
                f"{ENV_PREFIX}_STRIPE_API_KEY",
                purpose="the payment provider this deployment registers customers and bills with",
            )
        return build_client(
            api_key=self.api_key.get_secret_value(),
            timeout_seconds=self.timeout_seconds,
        )


__all__ = ["StripeSettings"]
