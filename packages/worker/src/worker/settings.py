from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import ENV_PREFIX


class ContainerServiceSettings(BaseSettings):
    token: SecretStr = SecretStr("")

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_CONTAINER_SERVICE_",
        env_file=".env",
        extra="ignore",
    )


__all__ = ["ContainerServiceSettings"]
