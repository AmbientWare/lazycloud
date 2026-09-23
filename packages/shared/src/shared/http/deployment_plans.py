from __future__ import annotations

from uuid import UUID

from pydantic import Field, field_validator, model_validator

from shared.app_slug import validate_app_slug
from shared.deployments import DeploymentKind
from shared.enums import StringEnum
from shared.http.base import HttpModel


class DeploymentPlanAction(StringEnum):
    Add = "add"
    Redeploy = "redeploy"
    Retain = "retain"
    Remove = "remove"


class WorkloadIdentity(HttpModel):
    kind: DeploymentKind
    name: str = Field(min_length=1, max_length=240)


class DeploymentPlanRequest(HttpModel):
    app: str
    workloads: list[WorkloadIdentity]
    prune: bool = False

    @field_validator("app")
    @classmethod
    def valid_app(cls, value: str) -> str:
        return validate_app_slug(value)

    @model_validator(mode="after")
    def unique_workloads(self) -> DeploymentPlanRequest:
        if len({(item.kind, item.name) for item in self.workloads}) != len(self.workloads):
            raise ValueError("workload kind and name must be unique within an app")
        return self


class DeploymentPlanItem(WorkloadIdentity):
    action: DeploymentPlanAction
    versions: int = Field(ge=0)


class DeploymentPlanResponse(HttpModel):
    app: str
    app_id: str | None
    snapshot: str
    prune: bool
    data: list[DeploymentPlanItem]
    next: str = ""


class DeploymentPruneRequest(DeploymentPlanRequest):
    operation_id: UUID
    app_id: str | None
    snapshot: str
    deployment_ids: list[UUID]
    prune: bool = True

    @model_validator(mode="after")
    def pruning_required(self) -> DeploymentPruneRequest:
        if not self.prune:
            raise ValueError("prune must be enabled")
        if len(set(self.deployment_ids)) != len(self.deployment_ids):
            raise ValueError("deployment IDs must be unique")
        return self


class DeploymentPruneResponse(HttpModel):
    operation_id: UUID
    app: str
    removed_versions: int = Field(ge=0)
    complete: bool
