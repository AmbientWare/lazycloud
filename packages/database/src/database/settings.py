from __future__ import annotations

from enum import StrEnum

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import ENV_PREFIX
from shared.deployment_settings import MissingDeploymentSettingError


class DatabaseApplicationName(StrEnum):
    Api = "lazycloud-api"
    Scheduler = "lazycloud-scheduler"
    ConnectionGateway = "lazycloud-connection-gateway"
    WorkerBootstrap = "lazycloud-worker-bootstrap"
    Admin = "lazycloud-admin"
    Bootstrap = "lazycloud-database-bootstrap"
    Wait = "lazycloud-database-wait"
    Test = "lazycloud-test"


class DatabaseSettings(BaseSettings):
    # Blank marks "nobody said", not a database. Which database a process reads
    # and writes is a deployment fact, so there is nothing to fall back to.
    url: str = ""
    direct_url: str = ""
    echo: bool = False
    pool_size: int = 5
    max_overflow: int = 10
    pool_timeout_seconds: float = 3.0
    """How long a caller waits for a connection before saying it cannot have one.

    SQLAlchemy's thirty-second default parks a request thread for longer than
    any health check will wait, so an exhausted pool arrives as a process that
    stopped answering rather than as a database that is busy. Failing fast is
    what makes the difference legible, and what lets the caller shed load
    instead of joining the queue that caused it.
    """
    pool_recycle_seconds: int = 900
    """Retire connections on a schedule instead of discovering them stale.

    A managed database reaps its own idle backends, and without this the first
    request after that reaping pays for the discovery.
    """
    pool_use_lifo: bool = True
    """Reuse the warmest connection, and let the cold tail expire.

    The server's connection ceiling is shared with every other process in the
    deployment. Round-robin keeps every slot alive forever, so a pool sized for
    a burst holds that many connections during the quiet hours too; taking the
    most recent one back lets the rest lapse under `pool_recycle_seconds` and
    leaves the ceiling to whoever is actually working.
    """
    connect_timeout_seconds: int = 10
    statement_timeout_ms: int = 30_000
    application_name: DatabaseApplicationName

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_DATABASE_",
        extra="ignore",
        hide_input_in_errors=True,
    )

    @model_validator(mode="after")
    def require_url(self) -> DatabaseSettings:
        if not self.url.strip():
            raise MissingDeploymentSettingError(
                f"{ENV_PREFIX}_DATABASE_URL",
                purpose="the control-plane PostgreSQL database this process reads and writes",
            )
        return self

    def direct(self) -> DatabaseSettings:
        if not self.direct_url.strip():
            raise MissingDeploymentSettingError(
                f"{ENV_PREFIX}_DATABASE_DIRECT_URL",
                purpose="direct PostgreSQL connections for administration and session locks",
            )
        return self.model_copy(update={"url": self.direct_url})
