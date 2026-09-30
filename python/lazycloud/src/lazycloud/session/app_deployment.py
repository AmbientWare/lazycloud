from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID, uuid4

from shared.http.deployment_plans import (
    DeploymentPlanRequest,
    DeploymentPlanResponse,
    DeploymentPruneRequest,
    DeploymentPruneResponse,
)
from shared.http.gateway import DeployStubResponse

from lazycloud.terminal import Terminal


class AppDeploymentControl(Protocol):
    def plan_deployment(self, request: DeploymentPlanRequest) -> DeploymentPlanResponse: ...
    def prune_deployments(self, request: DeploymentPruneRequest) -> DeploymentPruneResponse: ...


@dataclass(frozen=True)
class AppDeploymentTarget:
    manifest: DeploymentPlanRequest
    submit: Callable[[], tuple[DeployStubResponse, ...]]


@dataclass(frozen=True)
class AppDeploymentOutcome:
    app: str
    resources: tuple[DeployStubResponse, ...]
    pruning: DeploymentPruneResponse | None = None


@dataclass(frozen=True)
class AppDeploymentSession:
    control: AppDeploymentControl
    terminal: Terminal = field(default_factory=Terminal)

    def preview(self, targets: Sequence[AppDeploymentTarget]) -> list[DeploymentPlanResponse]:
        return [self.control.plan_deployment(target.manifest) for target in targets]

    def deploy(self, targets: Sequence[AppDeploymentTarget]) -> list[AppDeploymentOutcome]:
        plans = {
            target.manifest.app: self.control.plan_deployment(target.manifest)
            for target in targets
            if target.manifest.prune
        }
        # Finish every submission before any app can lose an omitted workload.
        submitted = [(target, target.submit()) for target in targets]
        outcomes: list[AppDeploymentOutcome] = []
        for target, resources in submitted:
            manifest = target.manifest
            pruning = None
            if manifest.prune:
                plan = plans[manifest.app]
                request = DeploymentPruneRequest(
                    operation_id=uuid4(),
                    app=manifest.app,
                    workloads=manifest.workloads,
                    app_id=plan.app_id,
                    snapshot=plan.snapshot,
                    deployment_ids=[UUID(resource.deployment_id) for resource in resources],
                )
                with self.terminal.step("Prune", manifest.app) as step:
                    pruning = self.control.prune_deployments(request)
                    step.done(f"{pruning.removed_versions} deployment versions removed")
            outcomes.append(AppDeploymentOutcome(manifest.app, resources, pruning))
        return outcomes
