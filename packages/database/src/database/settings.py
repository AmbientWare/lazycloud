from __future__ import annotations

from enum import StrEnum

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import ENV_PREFIX
from shared.deployment_settings import MissingDeploymentSettingError


class DatabaseApplicationName(StrEnum):
    Api = "lazycloud-api"
    Scheduler = "lazycloud-scheduler"
    WorkerBootstrap = "lazycloud-worker-bootstrap"
    Admin = "lazycloud-admin"
    Bootstrap = "lazycloud-database-bootstrap"
    Wait = "lazycloud-database-wait"
    Test = "lazycloud-test"


class DatabaseSettings(BaseSettings):
    # Blank marks "nobody said", not a database. Which database a process reads
    # and writes is a deployment fact, so there is nothing to fall back to.
    url: str = ""
    echo: bool = False
    pool_size: int = 5
    max_overflow: int = 10
    connect_timeout_seconds: int = 10
    statement_timeout_ms: int = 30_000
    application_name: DatabaseApplicationName

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_DATABASE_",
        extra="ignore",
    )

    @model_validator(mode="after")
    def require_url(self) -> DatabaseSettings:
        if not self.url.strip():
            raise MissingDeploymentSettingError(
                f"{ENV_PREFIX}_DATABASE_URL",
                purpose="the control-plane PostgreSQL database this process reads and writes",
            )
        return self
