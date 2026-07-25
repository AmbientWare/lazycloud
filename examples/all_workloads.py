"""One production app covering every public deployable workload kind.

Deploy the app from the repository root:

    uv run lazycloud deploy examples.all_workloads:app --workspace default

Then use the named helpers with ``lazycloud run`` to create representative
Runs. See ``examples/README.md`` for the complete command list.
"""

from __future__ import annotations

import json
import os
import time
from typing import TypedDict

from lazycloud.json_contracts import JsonValue

from examples.asgi import ASGIMessage, ASGIReceive, ASGISend
from lazycloud import App, Client, Image

APP_NAME_ENV = "LAZYCLOUD_ALL_WORKLOADS_APP_NAME"
APP_NAME = os.getenv(APP_NAME_ENV, "all_workloads")

image = Image(python_version="3.12")
app = App(APP_NAME)


class CalculationResult(TypedDict):
    input: int
    result: int
    status: str


class NestedCalculationResult(TypedDict):
    child: CalculationResult
    nested: bool


class PredictionResult(TypedDict):
    input: int
    prediction: int


class JobResult(TypedDict):
    accepted: bool
    value: int


@app.function(
    name="calculate",
    image=image,
    cpu=0.25,
    memory="128Mi",
)
def calculate(
    value: int = 7,
    fail: bool = False,
    delay_seconds: float = 0,
) -> CalculationResult:
    if delay_seconds > 0:
        time.sleep(delay_seconds)
    if fail:
        raise ValueError(f"intentional example failure for {value}")
    result = value * value
    print(json.dumps({"event": "calculated", "input": value, "result": result}), flush=True)
    return {"input": value, "result": result, "status": "complete"}


@app.function(
    name="nested-calculation",
    image=image,
    cpu=0.25,
    memory="128Mi",
    env={APP_NAME_ENV: APP_NAME},
)
def nested_calculation(value: int = 6) -> NestedCalculationResult:
    child = calculate(value)
    return {"child": child, "nested": True}


@app.endpoint(
    name="predict",
    route="/predict",
    methods=["POST"],
    image=image,
    cpu=0.25,
    memory="128Mi",
    keep_warm=0,
)
def predict(value: int = 7, fail: bool = False) -> PredictionResult:
    if fail:
        raise RuntimeError(f"intentional endpoint failure for {value}")
    return {"input": value, "prediction": value + 1}


@app.asgi(
    name="service",
    route="/service",
    image=image,
    cpu=0.25,
    memory="128Mi",
    keep_warm_seconds=0,
)
async def service(
    scope: ASGIMessage,
    receive: ASGIReceive,
    send: ASGISend,
) -> None:
    del receive
    body = json.dumps({"status": "ok", "path": scope.get("path", "/")}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})


@app.task_queue(
    name="jobs",
    image=image,
    cpu=0.25,
    memory="128Mi",
    workers=1,
    keep_warm_seconds=0,
    retries=1,
    retry_for=[RuntimeError],
)
def jobs(value: int = 5, fail: bool = False, delay_seconds: float = 0) -> JobResult:
    if delay_seconds > 0:
        time.sleep(delay_seconds)
    if fail:
        raise RuntimeError(f"intentional task queue failure for {value}")
    print(json.dumps({"event": "job-complete", "value": value}), flush=True)
    return {"accepted": True, "value": value}


@app.cron(
    "every 1m",
    name="heartbeat",
    image=image,
    cpu=0.25,
    memory="128Mi",
)
def heartbeat() -> dict[str, str]:
    print("all-workloads-heartbeat", flush=True)
    return {"status": "healthy"}


web = app.pod(
    name="web",
    image=image,
    command=["python", "-m", "http.server", "8080"],
    ports={"http": 8080},
    cpu=0.25,
    memory="128Mi",
    keep_warm=0,
)

# Sandboxes are created on demand, not deployed by ``app.deploy()``.
sandbox = app.sandbox(
    name="workspace",
    image=image,
    cpu=0.25,
    memory="128Mi",
    keep_warm_seconds=600,
    ports=[8080],
)


def run_function(value: int = 7) -> dict[str, JsonValue]:
    client = Client()
    target = client.deployment.resolve_target(
        kind=calculate.spec().kind,
        name=calculate.resource_name,
        app=APP_NAME,
    )
    submission = client.submit_deployment(target.deployment_id, value)
    if submission.task is None:
        raise RuntimeError("function deployment did not return a task handle")
    result = submission.task.wait(timeout_seconds=120, poll_interval_seconds=0.5)
    if not result.ok:
        raise RuntimeError(result.error or "function example failed")
    return {"task_id": submission.task_id, "result": result.task.result}


def run_function_failure(value: int = 13) -> dict[str, JsonValue]:
    client = Client()
    target = client.deployment.resolve_target(
        kind=calculate.spec().kind,
        name=calculate.resource_name,
        app=APP_NAME,
    )
    submission = client.submit_deployment(
        target.deployment_id,
        value,
        kwargs={"fail": True},
    )
    if submission.task is None:
        raise RuntimeError("function deployment did not return a task handle")
    result = submission.task.wait(timeout_seconds=120, poll_interval_seconds=0.5)
    if result.ok:
        raise RuntimeError("intentional failure example completed successfully")
    return {
        "task_id": submission.task_id,
        "status": result.status.value,
        "error": result.error,
        "expected_failure": True,
    }


def run_nested_function(value: int = 6) -> dict[str, JsonValue]:
    client = Client()
    target = client.deployment.resolve_target(
        kind=nested_calculation.spec().kind,
        name=nested_calculation.resource_name,
        app=APP_NAME,
    )
    submission = client.submit_deployment(target.deployment_id, value)
    if submission.task is None:
        raise RuntimeError("nested function deployment did not return a task handle")
    result = submission.task.wait(timeout_seconds=120, poll_interval_seconds=0.5)
    if not result.ok:
        raise RuntimeError(result.error or "nested function example failed")
    return {"task_id": submission.task_id, "result": result.task.result}


def run_task_queue(value: int = 5, delay_seconds: float = 0) -> dict[str, JsonValue]:
    handle = jobs.target("deployed").put(value, delay_seconds=delay_seconds)
    result = handle.result(wait=True, timeout_seconds=120, poll_interval_seconds=0.5)
    return {
        "task_id": handle.task_id,
        "status": result.status.value,
        "ok": result.ok,
        "result": result.task.result,
    }


def run_task_queue_failure(value: int = 17) -> dict[str, JsonValue]:
    handle = jobs.target("deployed").put(value, fail=True)
    result = handle.result(wait=True, timeout_seconds=120, poll_interval_seconds=0.5)
    if result.ok:
        raise RuntimeError("intentional task queue failure completed successfully")
    return {
        "task_id": handle.task_id,
        "status": result.status.value,
        "error": result.error,
        "expected_failure": True,
    }


def exercise_runs(value: int = 7) -> dict[str, JsonValue]:
    """Create successful, failed, nested, and task-queue Runs."""
    return {
        "function": run_function(value),
        "failure": run_function_failure(value),
        "nested": run_nested_function(value),
        "task_queue": run_task_queue(value),
        "task_queue_failure": run_task_queue_failure(value),
    }


def run_endpoint(value: int = 7, fail: bool = False) -> dict[str, JsonValue]:
    response = predict.target("deployed").request(value, fail=fail)
    return {"status_code": response.status_code, "result": response.json()}


def run_asgi() -> dict[str, JsonValue]:
    response = service.request(method="GET", path="/service", target="deployed")
    return {"status_code": response.status_code, "result": response.json()}


def create_pod() -> dict[str, str]:
    instance = web.create()
    return {"container_id": instance.container_id, "stub_id": instance.stub_id}


def create_sandbox() -> dict[str, str]:
    instance = sandbox.create()
    return {"container_id": instance.container_id, "stub_id": instance.stub_id}


__all__ = [
    "APP_NAME",
    "app",
    "calculate",
    "create_pod",
    "create_sandbox",
    "exercise_runs",
    "heartbeat",
    "jobs",
    "nested_calculation",
    "predict",
    "run_asgi",
    "run_endpoint",
    "run_function",
    "run_function_failure",
    "run_nested_function",
    "run_task_queue",
    "run_task_queue_failure",
    "sandbox",
    "service",
    "web",
]
