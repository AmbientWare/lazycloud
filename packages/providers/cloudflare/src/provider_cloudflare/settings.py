from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from provider_cloudflare.custom_hostnames import CloudflareCustomHostnames, build_client
from shared.app_identity import ENV_PREFIX


class CloudflareSettings(BaseSettings):
    """Credentials and zone for the edge that terminates customer TLS.

    The token is the only thing here that grants anything, so it is a `SecretStr`:
    settings objects reach logs and error reports, and a token that appears in one is
    a token that has to be rotated.
    """

    api_token: SecretStr = SecretStr("")
    zone_id: str = ""
    timeout_seconds: float = Field(default=30.0, gt=0)

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_CLOUDFLARE_",
        extra="ignore",
    )

    @property
    def configured(self) -> bool:
        return bool(self.api_token.get_secret_value() and self.zone_id)

    def provider(self) -> CloudflareCustomHostnames:
        """Build the adapter, refusing to pretend when nothing was configured.

        Named rather than silently returning a no-op: a deployment missing these is
        a deployment that cannot serve customer domains, and saying so once here beats
        every caller discovering it as a hostname that never verifies.
        """

        if not self.configured:
            raise ValueError(
                f"custom domains require {ENV_PREFIX}_CLOUDFLARE_API_TOKEN and "
                f"{ENV_PREFIX}_CLOUDFLARE_ZONE_ID"
            )
        return CloudflareCustomHostnames(
            client=build_client(
                api_token=self.api_token.get_secret_value(),
                timeout_seconds=self.timeout_seconds,
            ),
            zone_id=self.zone_id,
        )


__all__ = ["CloudflareSettings"]
