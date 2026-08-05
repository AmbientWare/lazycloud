from __future__ import annotations

from shared.deployment_records import Deployment
from shared.http.deployments import (
    DeploymentActionCapabilitiesResponse,
    DeploymentResourcesResponse,
    DeploymentResponse,
    DeploymentScalingResponse,
    DeploymentSpecResponse,
)


def deployment_response(
    deployment: Deployment,
    *,
    scaling: DeploymentScalingResponse | None = None,
    actions: DeploymentActionCapabilitiesResponse | None = None,
) -> DeploymentResponse:
    spec = deployment.spec
    return DeploymentResponse(
        id=deployment.id,
        name=deployment.name,
        kind=deployment.kind,
        app_id=deployment.app_id,
        stub_id=deployment.stub_id,
        version=deployment.version,
        spec=DeploymentSpecResponse(
            resources=DeploymentResourcesResponse.model_validate(spec.resources),
            route=spec.route,
            methods=spec.methods,
            cron=spec.cron,
            command=spec.command,
            ports=spec.ports,
            pool=deployment.pool,
        ),
        active=deployment.active,
        deleted_at=deployment.deleted_at,
        created_at=deployment.created_at,
        updated_at=deployment.updated_at,
        scaling=scaling,
        actions=actions or DeploymentActionCapabilitiesResponse(),
    )


__all__ = ["deployment_response"]
