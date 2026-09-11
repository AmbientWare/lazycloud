from __future__ import annotations

import asyncio
from typing import Any

import pytest
from lazycloud.abstractions.endpoint import EndpointResponse
from lazycloud.client_handles import (
    EndpointHandle,
    FunctionHandle,
    ResourceManifest,
)
from lazycloud.values import cloudpickle_bytes
from shared.deployments import DeploymentKind
from shared.function_payloads import FunctionCloudpickleResult, FunctionJsonResult
from shared.http.functions import (
    FunctionInvokeResponse,
)
from shared.http.tasks import TaskDetailResponse
from shared.tasks import Task, TaskStatus


def test_function_handle_async_remote_json_preserves_json_result_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_json_request(*args: Any, **kwargs: Any) -> dict[str, Any]:
        _ = args
        calls.append(kwargs["json_body"])
        return FunctionInvokeResponse.from_result(
            task_id="task-square",
            result=FunctionJsonResult(value=100),
            done=True,
        ).model_dump(mode="json")

    monkeypatch.setattr("lazycloud.client_handles._json_request", fake_json_request)
    handle = FunctionHandle(
        ResourceManifest(
            app="demo",
            name="square",
            kind=DeploymentKind.Function,
            stub_id="stub-square",
            deployment_id="dep-square",
            deployment_version=1,
            invoke_url="https://api.example/function/stub-square",
        )
    )

    assert handle.remote_json(value=10) == 100
    assert asyncio.run(handle.async_remote_json(value=10)) == 100
    assert calls == [
        {
            "args": [],
            "kwargs": {"value": 10},
            "result_format": "json",
        },
        {
            "args": [],
            "kwargs": {"value": 10},
            "result_format": "json",
        },
    ]


def test_function_handle_remote_json_decodes_deferred_task_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_json_request(*args: Any, **kwargs: Any) -> dict[str, Any]:
        _ = args, kwargs
        return FunctionInvokeResponse.from_result(task_id="task-square").model_dump(mode="json")

    class CompletedTaskClient:
        def __init__(self, **kwargs: Any) -> None:
            _ = kwargs

        def get_result_task(self, task_id: str) -> Task:
            return Task(
                id=task_id,
                name="function-square",
                status=TaskStatus.Complete,
                result=FunctionJsonResult(value=49).model_dump(mode="json"),
                exit_code=0,
            )

        def get(self, task_id: str) -> TaskDetailResponse:
            return TaskDetailResponse.model_validate(self.get_result_task(task_id))

    monkeypatch.setattr("lazycloud.client_handles._json_request", fake_json_request)
    monkeypatch.setattr("lazycloud.client_handles.TaskClient", CompletedTaskClient)
    handle = FunctionHandle(
        ResourceManifest(
            app="demo",
            name="square",
            kind=DeploymentKind.Function,
            stub_id="stub-square",
            deployment_id="dep-square",
            deployment_version=1,
            invoke_url="https://api.example/function/stub-square",
        )
    )

    assert handle.remote_json(7) == 49


def test_function_handle_remote_decodes_cloudpickled_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_json_request(*args: Any, **kwargs: Any) -> dict[str, Any]:
        _ = args, kwargs
        return FunctionInvokeResponse.from_result(
            task_id="task-bytes",
            result=FunctionCloudpickleResult.from_bytes(cloudpickle_bytes(b"result-bytes")),
            done=True,
        ).model_dump(mode="json")

    monkeypatch.setattr("lazycloud.client_handles._json_request", fake_json_request)
    handle = FunctionHandle(
        ResourceManifest(
            app="demo",
            name="bytes-value",
            kind=DeploymentKind.Function,
            stub_id="stub-bytes",
            deployment_id="dep-bytes",
            deployment_version=1,
            invoke_url="https://api.example/function/stub-bytes",
        )
    )

    assert handle.remote() == b"result-bytes"


def test_endpoint_handle_async_request_uses_request_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_endpoint_request(*args: Any, **kwargs: Any) -> EndpointResponse:
        _ = args
        calls.append(kwargs)
        return EndpointResponse(
            status_code=200,
            headers={},
            content=b'{"ok": true}',
            url="https://api.example/endpoint/public/stub-health",
        )

    monkeypatch.setattr("lazycloud.client_handles._endpoint_request", fake_endpoint_request)
    handle = EndpointHandle(
        ResourceManifest(
            app="demo",
            name="health",
            kind=DeploymentKind.Endpoint,
            stub_id="stub-health",
            deployment_id="dep-health",
            deployment_version=1,
            invoke_url="https://api.example/endpoint/public/stub-health",
            methods=("POST",),
        )
    )

    response = asyncio.run(handle.async_request(user_id=10))

    assert response.json() == {"ok": True}
    assert calls[0]["method"] == "POST"
    assert calls[0]["json_body"] == {"args": [], "kwargs": {"user_id": 10}}
