from __future__ import annotations

from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.urls import normalize_http_origin


class GatewaySettings(BaseSettings):
    public_http_url: str = Field(
        default="http://localhost:9000",
        validation_alias="LAZYCLOUD_GATEWAY_PUBLIC_HTTP_URL",
    )
    runtime_callback_http_url: str = Field(
        default="http://127.0.0.1:9000",
        validation_alias="LAZYCLOUD_GATEWAY_RUNTIME_HTTP_URL",
    )

    model_config = SettingsConfigDict(
        extra="ignore",
        populate_by_name=True,
    )

    @field_validator("public_http_url", "runtime_callback_http_url")
    @classmethod
    def normalize_url(cls, value: str) -> str:
        return normalize_http_origin(value, field_name="gateway HTTP URL")

    @property
    def public_base_domain(self) -> str:
        """The host every generated resource hostname sits one label under.

        Derived from the public URL rather than configured beside it: two settings
        that must agree eventually disagree, and the one that loses sends traffic to
        a name nothing answers on.
        """

        return (urlparse(self.public_http_url).hostname or "").lower()


__all__ = ["GatewaySettings"]
