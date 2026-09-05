from __future__ import annotations

from urllib.parse import urlparse

from networking.wireguard import WIREGUARD_GATEWAY_ADDRESS, WIREGUARD_RUNTIME_SERVICE_PORT
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.deployment_settings import MissingDeploymentSettingError
from shared.urls import normalize_http_origin

PUBLIC_HTTP_URL_VARIABLE = "LAZYCLOUD_GATEWAY_PUBLIC_HTTP_URL"


class GatewaySettings(BaseSettings):
    # Blank marks "nobody said", not an origin. This one is quoted back to
    # people and to other systems: it is the OAuth redirect GitHub matches, the
    # origin in the authorization template a customer applies to their own AWS
    # account, and the address a node enrols against. A localhost default would
    # be accepted everywhere and correct nowhere.
    public_http_url: str = Field(default="", validation_alias=PUBLIC_HTTP_URL_VARIABLE)
    runtime_callback_http_url: str = Field(
        default=f"http://{WIREGUARD_GATEWAY_ADDRESS}:{WIREGUARD_RUNTIME_SERVICE_PORT}",
        validation_alias="LAZYCLOUD_GATEWAY_RUNTIME_HTTP_URL",
    )

    model_config = SettingsConfigDict(
        extra="ignore",
        populate_by_name=True,
    )

    @field_validator("public_http_url", "runtime_callback_http_url")
    @classmethod
    def normalize_url(cls, value: str) -> str:
        if not value.strip():
            return value
        return normalize_http_origin(value, field_name="gateway HTTP URL")

    @model_validator(mode="after")
    def require_public_http_url(self) -> GatewaySettings:
        if not self.public_http_url.strip():
            raise MissingDeploymentSettingError(
                PUBLIC_HTTP_URL_VARIABLE,
                purpose="the public origin this deployment is reached on",
            )
        if not self.runtime_callback_http_url:
            raise MissingDeploymentSettingError(
                "LAZYCLOUD_GATEWAY_RUNTIME_HTTP_URL",
                purpose="the private API service reached through WireGuard",
            )
        return self

    @property
    def public_base_domain(self) -> str:
        """The host every generated resource hostname sits one label under.

        Derived from the public URL rather than configured beside it: two settings
        that must agree eventually disagree, and the one that loses sends traffic to
        a name nothing answers on.
        """

        return (urlparse(self.public_http_url).hostname or "").lower()


__all__ = ["PUBLIC_HTTP_URL_VARIABLE", "GatewaySettings"]
