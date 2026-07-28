from __future__ import annotations

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from scheduler.service import MANAGED_COMPUTE_RECONCILE_INTERVAL_SECONDS
from shared.app_identity import ENV_PREFIX
from shared.capacity import (
    CapacityOwnerIdentity,
    CapacityOwnerKind,
    CapacityOwnerSource,
    CapacityPoolPolicy,
)


class KubernetesCapacityPoolSettings(CapacityOwnerIdentity, CapacityPoolPolicy):
    pool_name: str

    @model_validator(mode="after")
    def require_kubernetes_ownership(self) -> KubernetesCapacityPoolSettings:
        if (
            self.capacity_owner_kind is not CapacityOwnerKind.GlobalKubernetesDeployment
            or self.capacity_owner_source is not CapacityOwnerSource.Kubernetes
        ):
            raise ValueError("scheduler Kubernetes pools require global Deployment ownership")
        return self


class SchedulerProcessSettings(BaseSettings):
    worker_pool_scaler_kubernetes_name: str = ""
    worker_pool_scaler_kubernetes_namespace: str = ""
    kubernetes_capacity_pools: tuple[KubernetesCapacityPoolSettings, ...] = ()
    managed_compute_reconcile_interval_seconds: float = Field(
        default=MANAGED_COMPUTE_RECONCILE_INTERVAL_SECONDS,
        gt=0,
    )

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_",
        extra="ignore",
    )

    @field_validator(
        "worker_pool_scaler_kubernetes_name",
        "worker_pool_scaler_kubernetes_namespace",
    )
    @classmethod
    def normalize_kubernetes_target(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_kubernetes_pools(self) -> SchedulerProcessSettings:
        names = [pool.pool_name for pool in self.kubernetes_capacity_pools]
        if len(names) != len(set(names)):
            raise ValueError("scheduler Kubernetes pool names must be unique")
        owner_ids = [pool.capacity_owner_id for pool in self.kubernetes_capacity_pools]
        if len(owner_ids) != len(set(owner_ids)):
            raise ValueError("scheduler Kubernetes capacity owners must be unique")
        if sum(pool.default_eligible for pool in self.kubernetes_capacity_pools) > 1:
            raise ValueError("only one Kubernetes capacity pool may accept unselected work")
        return self


__all__ = ["KubernetesCapacityPoolSettings", "SchedulerProcessSettings"]
