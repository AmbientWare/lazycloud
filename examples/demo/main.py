from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path

from lazycloud.clients.artifact.control import ArtifactControlClient
from lazycloud.control import resolve_control_client_config
from lazycloud.http_transport import request_raw
from lazycloud.json_contracts import parse_json_value
from pydantic import BaseModel, JsonValue
from shared.http.compute import MachineJoinCommandRequest

from lazycloud import (
    App,
    Artifact,
    Client,
    Image,
    Map,
    Queue,
    Secret,
    Task,
    Volume,
    experimental,
    schema,
)

demo = App("demo")
Signal = experimental.Signal
EXAMPLE_ENV = "LAZYCLOUD_EXAMPLE"

demo_image = Image(
    python_version="3.12",
    python_packages=["pydantic"],
    env_vars={EXAMPLE_ENV: "demo"},
)


def dockerfile_image(dockerfile: Path, context_dir: Path) -> Image:
    return (
        Image.from_dockerfile(dockerfile, context_dir=context_dir)
        .add_local_path("src")
        .add_python_packages(["httpx"])
        .with_envs({EXAMPLE_ENV: "dockerfile"})
    )


def machine_join_command(ttl: str = "30m", gpu: list[str] | None = None) -> str:
    """Return the command that attaches this workspace's own hardware."""
    request = MachineJoinCommandRequest(ttl=ttl, gpu=list(gpu or []))
    return Client().compute.machine_join_command(request).command


@demo.function(
    name="square",
    image=demo_image,
    cpu=1.0,
    memory="256Mi",
    inputs=schema.Schema({"value": schema.Integer()}),
    outputs=schema.Schema({"result": schema.Integer()}),
)
def square(value: int) -> int:
    return value * value


@demo.function(name="stream-count", image=demo_image, cpu=1.0, memory="256Mi")
def stream_count(count: int = 10, delay_seconds: float = 1.0) -> int:
    for index in range(count):
        print(index, flush=True)
        time.sleep(delay_seconds)
    return count


@demo.cron("every 1m", name="cron-marker", image=demo_image, cpu=1.0, memory="256Mi")
def cron_marker() -> int:
    print("cron-marker-ok", flush=True)
    return 21


@demo.function(name="nested-square", image=demo_image, cpu=1.0, memory="256Mi")
def nested_square(value: int) -> int:
    return int(square(value))


@demo.function(name="io-smoke", image=demo_image, cpu=1.0, memory="256Mi")
def io_smoke(
    rounds: int = 10,
    payload_kib: int = 256,
    pause_seconds: float = 3.0,
) -> dict[str, int]:
    """Move real network and disk traffic so container I/O metrics show activity.

    Each round pushes and pops a payload through the gateway-backed queue
    (network) and fsyncs a multiple of it to local disk, pausing so several
    metric samples land while I/O is moving.
    """
    queue = Queue("demo-io-smoke")
    scratch = Path("/tmp/io-smoke.bin")
    payload = "x" * (payload_kib * 1024)
    network_bytes = 0
    disk_bytes = 0
    for _ in range(rounds):
        queue.put(payload)
        queue.pop()
        network_bytes += len(payload) * 2
        data = (payload * 8).encode("ascii")
        with scratch.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        disk_bytes += len(data)
        time.sleep(pause_seconds)
    scratch.unlink(missing_ok=True)
    return {
        "rounds": rounds,
        "network_bytes_moved": network_bytes,
        "disk_bytes_written": disk_bytes,
    }


resource_smoke_volume = Volume("demo-resource-smoke")


@demo.function(
    name="resource-smoke",
    image=demo_image,
    cpu=1.0,
    memory="256Mi",
    secrets=["DEMO_RESOURCE_TOKEN"],
    volumes=[resource_smoke_volume],
)
def resource_smoke(value: int = 8) -> dict[str, JsonValue]:
    token = os.environ.get("DEMO_RESOURCE_TOKEN", "")
    queue = Queue("demo-resource-smoke-queue")
    queue.pop()

    mapping = Map("demo-resource-smoke-map")
    mapping["result"] = {"input": value, "square": value * value}
    Signal("demo-resource-smoke-signal").set()

    volume_root = resource_smoke_volume.path()
    volume_root.mkdir(parents=True, exist_ok=True)
    volume_path = volume_root / "result.json"
    payload: dict[str, JsonValue] = {
        "input": value,
        "square": value * value,
        "queue_consumed": True,
        "secret_ok": token == "demo-token",
        "map_key_present": "result" in mapping,
    }
    volume_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    artifact_path = Path("/tmp/resource-smoke-artifact.json")
    artifact_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    saved_artifact = Artifact.file(artifact_path, content_type="application/json").save()

    return {
        **payload,
        "task_id": os.environ.get("TASK_ID", ""),
        "volume_path": str(volume_path),
        "volume_written": volume_path.exists(),
        "artifact_id": saved_artifact.artifact_id,
        "artifact_filename": saved_artifact.filename,
        "artifact_remote": saved_artifact.remote,
    }


@demo.function(name="signal-handler-smoke", image=demo_image, cpu=1.0, memory="256Mi")
def signal_handler_smoke() -> dict[str, JsonValue]:
    hits: list[JsonValue] = []
    signal = Signal(
        "demo-signal-handler-smoke",
        handler=lambda: hits.append("handled"),
        clear_after_interval=1,
        sleeper=lambda _: None,
    )
    signal.set(ttl=60)
    first = signal.monitor_once()
    second = signal.monitor_once()
    return {
        "first_set": first.set,
        "handler_hits": hits,
        "cleared_after_handler": not second.set,
    }


class SummarizeResult(BaseModel):
    count: int
    total: int


@demo.task_queue(
    name="summaries",
    image=demo_image,
    workers=2,
    max_pending_tasks=100,
    autoscaler={"max_containers": 3, "tasks_per_container": 1},
)
def summarize(values: list[int]) -> SummarizeResult:
    return SummarizeResult(count=len(values), total=sum(values))


def _summary_payload(result: SummarizeResult) -> dict[str, int]:
    return result.model_dump()


@demo.endpoint(name="health", route="/health", methods=["GET"], image=demo_image)
def health(print_str: str | None = None) -> dict[str, JsonValue]:
    print(print_str, flush=True) if print_str else None
    return {"status": "ok", "print_str": print_str}


scale_smoke_pod = demo.pod(
    name="scale-smoke-pod",
    image=demo_image,
    command=["python", "-m", "http.server", "8080"],
    ports={"http": 8080},
    keep_warm=0,
)


def run_remote_square(value: int = 8) -> int:
    return int(square.remote(value))


def run_remote_stream_count(count: int = 10, delay_seconds: float = 1.0) -> int:
    return int(stream_count.remote(count, delay_seconds))


def run_remote_task_queue_summary(values: list[int] | None = None) -> dict[str, JsonValue]:
    selected_values = values or [1, 4, 9, 16]
    deployment = summarize.deploy(name=f"demo-summaries-{int(time.time())}")
    if not deployment.deployment_id:
        msg = "task queue deploy failed"
        raise RuntimeError(msg)
    try:
        handle = summarize.put(selected_values)
        result = handle.result(wait=True, timeout_seconds=60, poll_interval_seconds=0.5)
        return {
            "deployment_id": deployment.deployment_id,
            "task_id": handle.task_id,
            "status": result.status.value,
            "result": result.task.result,
        }
    finally:
        Client().deployment.delete(deployment.deployment_id)


def run_remote_task_queue_autoscale_stress(
    batch_count: int = 6,
    batch_size: int = 3,
) -> dict[str, JsonValue]:
    if batch_count <= 0:
        msg = "batch_count must be positive"
        raise ValueError(msg)
    if batch_size <= 0:
        msg = "batch_size must be positive"
        raise ValueError(msg)
    deployment_name = f"autoscale-summaries-{int(time.time())}"
    deployment = summarize.deploy(name=deployment_name)
    if not deployment.deployment_id:
        msg = "task queue deploy failed"
        raise RuntimeError(msg)
    handles: list[Task] = []
    try:
        for batch_index in range(batch_count):
            start = batch_index * batch_size
            values = list(range(start, start + batch_size))
            handle = summarize.put(values)
            handles.append(handle)
        results = [
            handle.result(wait=True, timeout_seconds=120, poll_interval_seconds=0.5)
            for handle in handles
        ]
        payload: dict[str, JsonValue] = {
            "deployment_id": deployment.deployment_id,
            "stub_id": deployment.stub_id,
            "deployment_name": deployment_name,
            "task_count": len(results),
            "tasks": [
                {
                    "task_id": handle.task_id,
                    "status": result.status.value,
                    "ok": result.ok,
                    "result": result.task.result,
                }
                for handle, result in zip(handles, results, strict=True)
            ],
            "all_complete": all(result.ok for result in results),
        }
    except Exception as workflow_error:
        try:
            Client().deployment.stop(deployment.deployment_id)
        except Exception as cleanup_error:
            raise ExceptionGroup(
                "task-queue autoscale stress and deployment cleanup failed",
                [workflow_error, cleanup_error],
            ) from workflow_error
        raise
    return {**payload, "stopped": False}


def run_remote_nested_square(value: int = 8) -> int:
    return int(nested_square.remote(value))


def run_remote_io_smoke(rounds: int = 10, payload_kib: int = 256) -> dict[str, int]:
    return io_smoke.remote(rounds, payload_kib)


def _invoke_remote_resource_smoke(value: int) -> dict[str, JsonValue]:
    Secret("DEMO_RESOURCE_TOKEN").set("demo-token")
    resource_smoke_volume.create()
    Queue("demo-resource-smoke-queue").put({"value": value})
    Map("demo-resource-smoke-map").set("preflight", {"ready": True})
    Signal("demo-resource-smoke-signal").clear()
    return resource_smoke.remote(value)


def _cleanup_remote_resource_smoke() -> None:
    cleanups: tuple[Callable[[], bool | None], ...] = (
        Signal("demo-resource-smoke-signal").clear,
        Map("demo-resource-smoke-map").delete,
        Queue("demo-resource-smoke-queue").delete,
        resource_smoke_volume.delete,
        Secret("DEMO_RESOURCE_TOKEN").delete,
    )
    failures: list[Exception] = []
    for cleanup in cleanups:
        try:
            cleanup()
        except Exception as exc:
            failures.append(exc)
    if failures:
        raise ExceptionGroup("resource smoke cleanup failed", failures)


def run_remote_resource_smoke(value: int = 8) -> dict[str, JsonValue]:
    try:
        return _invoke_remote_resource_smoke(value)
    finally:
        _cleanup_remote_resource_smoke()


def run_remote_resource_smoke_verified(value: int = 8) -> dict[str, JsonValue]:
    try:
        result = _invoke_remote_resource_smoke(value)
        task_id = str(result.get("task_id") or "")
        artifact_id = str(result.get("artifact_id") or "")
        artifact_filename = str(result.get("artifact_filename") or "resource-smoke-artifact.json")
        artifact_client = _artifact_control_client()
        artifact_stat = artifact_client.stat(artifact_id, task_id, artifact_filename)
        artifact_url = artifact_client.public_url(
            artifact_id,
            task_id,
            artifact_filename,
        )
        response = request_raw(
            artifact_url.public_url,
            method="GET",
            timeout_seconds=resolve_control_client_config().timeout_seconds,
        )
        if not 200 <= response.status_code < 300:
            raise RuntimeError(f"artifact download failed with HTTP {response.status_code}")
        volume_stat = resource_smoke_volume.stat("result.json")
        artifact_stat_payload: dict[str, JsonValue] = {"ok": False}
        if artifact_stat.stat is not None:
            artifact_stat_payload = {
                "ok": True,
                "mode": artifact_stat.stat.mode,
                "size": artifact_stat.stat.size,
            }
        return {
            **result,
            "post_worker_volume_stat": {
                "path": volume_stat.path,
                "size": volume_stat.size,
                "is_dir": volume_stat.is_dir,
            },
            "post_worker_artifact_stat": artifact_stat_payload,
            "post_worker_artifact_url_ok": bool(artifact_url.public_url),
            "post_worker_artifact_payload": _json_or_text(response.content.decode("utf-8")),
        }
    finally:
        _cleanup_remote_resource_smoke()


def run_remote_signal_handler_smoke() -> dict[str, JsonValue]:
    Signal("demo-signal-handler-smoke").clear()
    return signal_handler_smoke.remote()


def _artifact_control_client() -> ArtifactControlClient:
    config = resolve_control_client_config()
    return ArtifactControlClient.from_endpoint(
        config.endpoint,
        token=config.token,
        timeout_seconds=config.timeout_seconds,
        workspace=config.workspace,
    )


def _json_or_text(value: str) -> JsonValue:
    try:
        return parse_json_value(value)
    except ValueError:
        return value


def run_remote_function_smoke(value: int = 8) -> dict[str, JsonValue]:
    return {
        "function": "square",
        "input": value,
        "result": run_remote_square(value),
    }


def main() -> None:
    print(json.dumps(run_remote_function_smoke(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
