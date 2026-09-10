from __future__ import annotations

from collections.abc import Awaitable, Callable

from tests.fakes import FakeDeploymentClient

from lazycloud import App


def test_app_deploy_applies_the_pool_to_every_deployable_resource() -> None:
    app = App("pool_deploy")
    deployments = FakeDeploymentClient(stub_id_from_type=True)

    function = app.function(lambda: "function", name="function")
    endpoint = app.endpoint(name="endpoint")(lambda: "endpoint")
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
    pod.deployment_client = deployments
    asgi.deployment_client = deployments

    result = app.deploy(
        workspace="production",
        pool="aws",
    )

    assert len(result.resources) == 4
    assert {request.name for request in deployments.stub_requests} == {
        "function",
        "endpoint",
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
