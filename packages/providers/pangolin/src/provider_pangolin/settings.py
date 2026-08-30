from __future__ import annotations

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import ENV_PREFIX

from provider_pangolin.client import PangolinClient


class PangolinSettings(BaseSettings):
    api_url: str = ""
    api_key: SecretStr = SecretStr("")
    organization_id: str = ""
    endpoint: str = ""
    platform_identity_prefix: str = "lazycloud-platform"
    platform_site_ids: tuple[int, ...] = ()
    platform_client_record_ids: tuple[int, ...] = ()
    platform_target_host: str = "control-plane"
    platform_target_port: int = Field(default=9000, ge=1, le=65535)
    timeout_seconds: float = Field(default=30.0, gt=0, le=120)

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_PANGOLIN_",
        extra="ignore",
    )

    @field_validator("api_url", "endpoint")
    @classmethod
    def normalize_url(cls, value: str) -> str:
        return value.strip().rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(
            self.api_url
            and self.api_key.get_secret_value()
            and self.organization_id
            and self.endpoint
        )

    def client(self) -> PangolinClient:
        if not self.configured:
            raise ValueError(
                f"Pangolin requires {ENV_PREFIX}_PANGOLIN_API_URL, "
                f"{ENV_PREFIX}_PANGOLIN_API_KEY, "
                f"{ENV_PREFIX}_PANGOLIN_ORGANIZATION_ID, "
                f"{ENV_PREFIX}_PANGOLIN_ENDPOINT"
            )
        return PangolinClient(
            api_url=self.api_url,
            api_key=self.api_key,
            organization_id=self.organization_id,
            endpoint=self.endpoint,
            platform_identity_prefix=self.platform_identity_prefix,
            platform_site_ids=self.platform_site_ids,
            platform_client_record_ids=self.platform_client_record_ids,
            platform_target_host=self.platform_target_host,
            platform_target_port=self.platform_target_port,
            timeout_seconds=self.timeout_seconds,
        )


__all__ = ["PangolinSettings"]
