from __future__ import annotations

from collections.abc import Awaitable, Callable

from lazycloud import App, ComputePlacementTarget


def test_all_executable_app_resources_preserve_explicit_placement() -> None:
    app = App("placement_app")

    function = app.function(
        lambda: "function", name="function", placement=ComputePlacementTarget.Aws
    )
    cron = app.cron("* * * * *", name="cron", placement=ComputePlacementTarget.Aws)(lambda: "cron")
    endpoint = app.endpoint(name="endpoint", placement=ComputePlacementTarget.Aws)(
        lambda: "endpoint"
    )
    task_queue = app.task_queue(name="queue", placement=ComputePlacementTarget.Aws)(lambda: "queue")
    pod = app.pod(name="pod", placement=ComputePlacementTarget.Aws)
    sandbox = app.sandbox(name="sandbox", placement=ComputePlacementTarget.Aws)

    async def asgi_app(
        scope: dict[str, object],
        receive: Callable[[], Awaitable[dict[str, object]]],
        send: Callable[[dict[str, object]], Awaitable[None]],
    ) -> None:
        del scope, receive, send

    asgi = app.asgi(name="asgi", placement=ComputePlacementTarget.Aws)(asgi_app)

    def realtime_handler(message: str) -> str:
        return message

    realtime = app.realtime(name="realtime", placement=ComputePlacementTarget.Aws)(realtime_handler)

    resources = (function, cron, endpoint, task_queue, pod, sandbox, asgi, realtime)
    assert all(resource.spec().placement is ComputePlacementTarget.Aws for resource in resources)


def test_omitted_placement_remains_unresolved_for_workspace_default() -> None:
    app = App("default_placement_app")
    function = app.function(lambda: "managed")

    assert function.spec().placement is None
