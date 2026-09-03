from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from scheduler.service import MANAGED_COMPUTE_RECONCILE_INTERVAL_SECONDS
from shared.app_identity import ENV_PREFIX


class SchedulerProcessSettings(BaseSettings):
    workload_image_registry_repository: str = ""
    managed_compute_reconcile_interval_seconds: float = Field(
        default=MANAGED_COMPUTE_RECONCILE_INTERVAL_SECONDS,
        gt=0,
    )

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_",
        extra="ignore",
    )


__all__ = ["SchedulerProcessSettings"]
