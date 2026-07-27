from __future__ import annotations

from enum import StrEnum

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import ENV_PREFIX, NAME


class DatabaseApplicationName(StrEnum):
    Api = "lazycloud-api"
    Scheduler = "lazycloud-scheduler"
    WorkerBootstrap = "lazycloud-worker-bootstrap"
    Admin = "lazycloud-admin"
    Bootstrap = "lazycloud-database-bootstrap"
    Wait = "lazycloud-database-wait"
    Test = "lazycloud-test"


class DatabaseSettings(BaseSettings):
    url: str = Field(
        default=f"postgresql+psycopg://{NAME}:{NAME}@localhost:5432/{NAME}",
        description="SQLAlchemy database URL for the production control-plane database.",
    )
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
