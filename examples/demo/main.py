from __future__ import annotations

import json
import os
import time
from pathlib import Path

from pydantic import BaseModel, JsonValue
from shared.http.compute import MachineJoinCommandRequest

from lazycloud import (
    App,
    Artifact,
    Client,
    Image,
    Map,
    Queue,
    Volume,
    current_task_id,
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


@demo.function(cron="every 1m", name="cron-marker", image=demo_image, cpu=1.0, memory="256Mi")
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
        "task_id": current_task_id(),
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


@demo.function(
    name="summaries",
    image=demo_image,
    concurrency=2,
    max_pending_tasks=100,
    autoscaler={"max_containers": 3, "tasks_per_container": 1},
)
def summarize(values: list[int]) -> SummarizeResult:
    return SummarizeResult(count=len(values), total=sum(values))


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
