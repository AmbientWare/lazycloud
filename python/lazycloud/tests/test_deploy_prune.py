from typing import Any, cast
from uuid import uuid4

from lazycloud.clients.api import ApiClient
from lazycloud.contracts.api import Deployment, DeploymentRequest
from lazycloud.session.deployment import AppFunctions, deploy_functions


class _Control:
    def __init__(self) -> None:
        self.calls: list[tuple[str, DeploymentRequest]] = []

    def deploy_app(self, workspace: str, app: str, request: DeploymentRequest) -> Deployment:
        self.calls.append((app, request))
        return Deployment.model_validate(
            {
                "app": {
                    "id": str(uuid4()),
                    "name": app,
                    "state": "active",
                    "workloads": 0,
                    "running_containers": 0,
                    "created_at": "2026-10-01T00:00:00Z",
                },
                "releases": [],
                "pruned": [],
                "removed_versions": 2,
            }
        )


def test_apps_that_only_prune_are_sent_only_the_prune_request(tmp_path: Any) -> None:
    control = _Control()
    targets = [
        AppFunctions(app="old", functions=(), prune=True),
        AppFunctions(app="gone", functions=(), prune=True),
    ]

    deployments = deploy_functions(
        targets, client=cast(ApiClient, control), workspace="ws", source_root=tmp_path
    )

    assert [(app, request.prune, request.workloads) for app, request in control.calls] == [
        ("old", True, []),
        ("gone", True, []),
    ]
    assert [deployment.app.name for deployment in deployments] == ["old", "gone"]
