from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from shared.http.gateway import DeployStubResponse
from tests.fakes import FakeDeploymentClient

from lazycloud import App


def test_app_deploy_forwards_source_root_to_function_deployment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_roots: list[Path | str | None] = []

    class RecordingDeploymentClient:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        def create(
            self,
            spec: object,
            *,
            name: str | None = None,
            workspace: str | None = None,
            image: object | None = None,
            source_root: Path | str | None = None,
        ) -> DeployStubResponse:
            del spec, name, workspace, image
            source_roots.append(source_root)
            return DeployStubResponse(stub_id="stub-function", deployment_id="deployment")

    monkeypatch.setattr(
        "lazycloud.abstractions.function.DeploymentClient",
        RecordingDeploymentClient,
    )
    app = App("source_root")
    app.function(lambda: "ok", name="function")

    result = app.deploy(source_root=tmp_path)

    assert len(result.resources) == 1
    assert source_roots == [tmp_path]


def test_app_deploy_applies_the_pool_to_every_deployable_resource() -> None:
    app = App("pool_deploy")
    deployments = FakeDeploymentClient(stub_id_from_type=True)

    function = app.function(lambda: "function", name="function")
    endpoint = app.endpoint(name="endpoint")(lambda: "endpoint")
    task_queue = app.task_queue(name="queue")(lambda: "queue")
    pod = app.pod(name="pod")

    async def asgi_handler(
        scope: dict[str, object],
        receive: Callable[[], Awaitable[dict[str, object]]],
        send: Callable[[dict[str, object]], Awaitable[None]],
    ) -> None:
        del scope, receive, send

    asgi = app.asgi(name="asgi")(asgi_handler)

    function.deployment_client = deployments
    endpoint.deployment_client = deployments
    task_queue.deployment_client = deployments
    pod.deployment_client = deployments
    asgi.deployment_client = deployments

    result = app.deploy(
        workspace="production",
        pool="aws",
    )

    assert len(result.resources) == 5
    assert {request.name for request in deployments.stub_requests} == {
        "function",
        "endpoint",
        "queue",
        "pod",
        "asgi",
    }
    assert all(
        request.workspace == "production" and request.pool == "aws"
        for request in deployments.stub_requests
    )


def test_app_deploy_pool_only_changes_the_selected_resource() -> None:
    app = App("selected_pool")
    deployments = FakeDeploymentClient(stub_id_from_type=True)
    function = app.function(
        lambda: "function",
        name="function",
    )
    pod = app.pod(name="pod", pool="lazycloud")
    function.deployment_client = deployments

    app.deploy(
        resource="function:function",
        pool="aws",
    )

    assert function.pool == "aws"
    assert pod.pool == "lazycloud"
    assert len(deployments.stub_requests) == 1
    assert deployments.stub_requests[0].pool == "aws"
