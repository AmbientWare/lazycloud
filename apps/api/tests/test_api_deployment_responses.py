from api.server.response_mapping import deployment_response
from shared.compute_policy import ComputePlacement, ComputePlacementSource, ComputePlacementTarget
from shared.deployment_records import Deployment, DeploymentSpec, Resources
from shared.deployments import DeploymentKind


def test_deployment_response_exposes_safe_workload_configuration() -> None:
    deployment = Deployment(
        id="deployment-safe-summary",
        name="predict",
        kind=DeploymentKind.Endpoint,
        app_id="app-1",
        stub_id="stub-1",
        spec=DeploymentSpec(
            name="predict",
            kind=DeploymentKind.Endpoint,
            route="/predict",
            methods=["post"],
            ports={"http": 8080},
            resources=Resources(
                cpu=2,
                memory="2Gi",
                timeout_seconds=180,
                concurrency=8,
                keep_warm=180,
            ),
            env={"MODEL_TOKEN": "must-not-leave-the-control-plane"},
            secrets=["production-model-token"],
            placement=ComputePlacementTarget.Aws,
        ),
        resolved_placement=ComputePlacement(
            target=ComputePlacementTarget.Aws,
            source=ComputePlacementSource.WorkloadOverride,
            provider="aws",
            region="us-east-1",
        ),
    )

    response = deployment_response(deployment).model_dump(mode="json")

    assert response["spec"] == {
        "resources": {
            "cpu": 2.0,
            "memory": "2Gi",
            "gpu": None,
            "gpu_count": 0,
            "timeout_seconds": 180,
            "concurrency": 8,
            "keep_warm": 180,
        },
        "route": "/predict",
        "methods": ["POST"],
        "cron": None,
        "command": [],
        "ports": {"http": 8080},
        "placement": {
            "target": "aws",
            "source": "workload_override",
            "provider": "aws",
            "region": "us-east-1",
        },
    }
    assert "env" not in response["spec"]
    assert "secrets" not in response["spec"]
