from __future__ import annotations

from typing import Protocol

from database.repositories.deployment_effects import DeploymentEffect
from observability.workspace_changes import WorkspaceChangePublisher
from shared.container_requests import ContainerShutdownTarget
from shared.workload_config import StubRuntimeConfig


class DeploymentPlacementResourceManager(Protocol):
    def reconcile_deployments(self, *, workspace: str, required: bool = True) -> None: ...


class DeploymentExecutionEffects(Protocol):
    def validate_pod_activation(self, runtime: StubRuntimeConfig) -> None: ...
    def stop_containers(
        self, targets: list[ContainerShutdownTarget], *, confirm: bool = True
    ) -> None: ...
    def delete_deployment_execution(
        self, *, workspace_id: str, deployment_ids: list[str]
    ) -> None: ...


class DeploymentEffects(Protocol):
    @property
    def containers(self) -> DeploymentExecutionEffects: ...
    @property
    def placement_resources(self) -> DeploymentPlacementResourceManager | None: ...
    @property
    def workspace_changes(self) -> WorkspaceChangePublisher | None: ...
    def reconcile_pending(self, *, limit: int = 25) -> None: ...
    def finish(self, effects: list[DeploymentEffect]) -> None: ...
