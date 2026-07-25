from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.urls import normalize_http_origin


class GatewaySettings(BaseSettings):
    public_http_url: str = Field(
        default="http://127.0.0.1:9000",
        validation_alias="LAZYCLOUD_GATEWAY_PUBLIC_HTTP_URL",
    )
    runtime_callback_http_url: str = Field(
        default="http://127.0.0.1:9000",
        validation_alias="LAZYCLOUD_GATEWAY_RUNTIME_HTTP_URL",
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
    )

    @field_validator("public_http_url", "runtime_callback_http_url")
    @classmethod
    def normalize_url(cls, value: str) -> str:
        return normalize_http_origin(value, field_name="gateway HTTP URL")


__all__ = ["GatewaySettings"]
