"""Observability acceptance against a running platform.

Runs only with LAZYCLOUD_TEST_ENDPOINT, LAZYCLOUD_TEST_TOKEN and
LAZYCLOUD_TEST_WORKSPACE naming a stack from `deploy/local/run.sh start`
(server, scheduler and an agent on Docker). It reads every operation the
dashboard uses for metrics, performance, task drawers and live updates.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import lazycloud.config
import pytest

ENDPOINT = os.environ.get("LAZYCLOUD_TEST_ENDPOINT", "")
TOKEN = os.environ.get("LAZYCLOUD_TEST_TOKEN", "")
WORKSPACE = os.environ.get("LAZYCLOUD_TEST_WORKSPACE", "")

pytestmark = [
    pytest.mark.skipif(not (ENDPOINT and TOKEN and WORKSPACE), reason="needs a running platform"),
    pytest.mark.usefixtures("isolated_imports"),
]

APP = """\
import time

import lazycloud

app = lazycloud.App("{app}")


@app.function(keep_warm=120, timeout_seconds=120)
def burn(seconds: float) -> int:
    spins = 0
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        spins += 1
    return spins


@app.function(keep_warm=120, timeout_seconds=120)
def leaf(x: int) -> int:
    return x * 2


@app.function(keep_warm=120, timeout_seconds=120)
def fan(n: int) -> list[int]:
    return [leaf.remote(i) for i in range(n)]
"""


class Stream:
    """The workspace change stream, read on a thread."""

    def __init__(self, last_event_id: str | None = None) -> None:
        self.events: list[tuple[str, str, dict[str, Any], float]] = []
        self._lock = threading.Lock()
        headers = {"Authorization": f"Bearer {TOKEN}", "Accept": "text/event-stream"}
        if last_event_id:
            headers["Last-Event-ID"] = last_event_id
        self._client = httpx.Client(timeout=httpx.Timeout(10, read=60))
        self._response = self._client.send(
            self._client.build_request("GET", f"{ENDPOINT}/v1/workspaces/{WORKSPACE}/changes/stream", headers=headers),
            stream=True,
        )
        assert self._response.status_code == 200, self._response.read()
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        event, ident, data = "", "", ""
        try:
            for line in self._response.iter_lines():
                if line.startswith("event: "):
                    event = line[7:]
                elif line.startswith("id: "):
                    ident = line[4:]
                elif line.startswith("data: "):
                    data = line[6:]
                elif line == "" and event:
                    with self._lock:
                        self.events.append((event, ident, json.loads(data), time.monotonic()))
                    event, ident, data = "", "", ""
        except httpx.HTTPError:
            return

    def wait(self, match: Any, timeout: float = 30) -> tuple[str, str, dict[str, Any], float]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                for event in self.events:
                    if match(event):
                        return event
            time.sleep(0.05)
        raise AssertionError(f"no matching change among {len(self.events)} events")

    def close(self) -> None:
        self._response.close()
        self._client.close()


def task_change(task_id: str, status: str) -> Any:
    def match(event: tuple[str, str, dict[str, Any], float]) -> bool:
        kind, _, data, _ = event
        return kind == "change" and any(
            c.get("task_id") == task_id and c.get("status") == status for c in data["changes"]
        )

    return match


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Iterator[httpx.Client]:
    monkeypatch.setenv("LAZYCLOUD_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("LAZYCLOUD_TOKEN", TOKEN)
    monkeypatch.setenv("LAZYCLOUD_WORKSPACE", WORKSPACE)
    lazycloud.config.reset_settings_cache()
    with httpx.Client(base_url=ENDPOINT, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=30) as client:
        yield client


def get(api: httpx.Client, path: str, **params: Any) -> Any:
    response = api.get(path, params=params)
    assert response.status_code == 200, (path, response.text)
    return response.json()


def _deploy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, ModuleType]:
    app = f"ob{uuid.uuid4().hex[:10]}"
    module = f"{app}_mod"
    (tmp_path / f"{module}.py").write_text(APP.format(app=app), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])
    loaded = importlib.import_module(module)
    loaded.app.deploy(source_root=tmp_path)
    return app, loaded


def test_observability_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, api: httpx.Client) -> None:
    ws = f"/v1/workspaces/{WORKSPACE}"
    stream = Stream()
    app, module = _deploy(tmp_path, monkeypatch)

    # Live updates: the stream reports the task's transitions as they commit.
    call = module.burn.spawn(8)
    task_id = call.task_id
    stream.wait(task_change(task_id, "queued"))
    first_id = stream.events[0][1]
    stream.wait(task_change(task_id, "running"), timeout=120)
    assert call.get(timeout_seconds=180) > 0
    finished_at = time.monotonic()
    _, _, _, seen_at = stream.wait(task_change(task_id, "succeeded"))
    print(f"change stream: succeeded event {max(seen_at - finished_at, 0) * 1000:.0f} ms after the result returned")
    stream.wait(lambda e: e[0] == "change" and any(c["topic"] == "containers" for c in e[2]["changes"]))

    # Resumption replays what followed the first event.
    resumed = Stream(first_id)
    resumed.wait(task_change(task_id, "succeeded"))
    resumed.close()

    # Task drawer: timeline, container lifecycle and metrics.
    timeline = get(api, f"{ws}/tasks/{task_id}/timeline")
    assert [e["kind"] for e in timeline["events"]] == ["submitted", "attempt_started", "attempt_finished", "finished"]
    container = timeline["events"][1]["container_id"]
    lifecycle = get(api, f"{ws}/containers/{container}/lifecycle")
    stages = [s["stage"] for s in lifecycle["stages"]]
    assert stages[:5] == ["placement", "image", "source", "create", "runtime"], stages
    print("start stages (ms):", {s["stage"]: s.get("duration_ms") for s in lifecycle["stages"]})
    metrics = get(api, f"{ws}/containers/{container}/metrics")
    assert metrics["step_seconds"] == 5 and metrics["cpu_total_millicores"] > 0
    busiest = max(p["cpu_millicores"] for p in metrics["points"])
    assert busiest > 500, metrics["points"]
    assert all(p["memory_rss_bytes"] > 0 for p in metrics["points"])
    print(f"container metrics: {len(metrics['points'])} points, peak {busiest:.0f} millicores")

    # Trace: the call graph from any of its tasks.
    fan = module.fan.spawn(3)
    assert fan.get(timeout_seconds=180) == [0, 2, 4]
    graph = get(api, f"{ws}/tasks/{fan.task_id}/call-graph")
    assert graph["root_task_id"] == fan.task_id and len(graph["nodes"]) == 4
    child = graph["nodes"][1]["task_id"]
    assert get(api, f"{ws}/tasks/{child}/call-graph")["nodes"] == graph["nodes"]
    batch = api.post(f"{ws}/containers/lifecycles", json={"container_ids": [n["container_id"] for n in graph["nodes"]]})
    assert batch.status_code == 200 and len(batch.json()["lifecycles"]) >= 2

    # Workload performance and workspace metrics.
    deployments = get(api, f"{ws}/deployments", app=app)["deployments"]
    burn = next(d for d in deployments if d["name"] == "burn")
    perf = get(api, f"{ws}/deployments/{burn['id']}/performance")
    bucket = perf["buckets"][-1]
    assert bucket["count"] == 1 and bucket["cold_starts"] == 1 and bucket["p50_ms"] >= 8000, perf
    task_metrics = get(api, f"{ws}/metrics/tasks", app=app)
    assert task_metrics["total"] == 5 and task_metrics["status_counts"]["succeeded"] == 5
    activity = get(api, f"{ws}/metrics/activity", app=app)
    assert {s["function"] for s in activity["series"]} == {"burn", "fan", "leaf"}
    assert sum(s["total"] for s in activity["series"]) == 5

    # Account metrics across the caller's workspaces.
    account = get(api, "/v1/me/metrics")
    assert account["containers"]["running"] >= 3 and account["concurrency"]["cpu_containers"] >= 3
    starts = get(api, "/v1/me/activity", measure="containers", window_seconds=900)
    assert any(s.get("app") == app and s["total"] == 3 for s in starts["series"]), starts
    cpu = get(api, "/v1/me/activity", measure="cpu", window_seconds=900)
    assert cpu["unit"] == "cores" and cpu["total"] > 0
    stream.close()
