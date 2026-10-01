"""Workload runtime acceptance against a running platform.

Runs only with LAZYCLOUD_TEST_ENDPOINT, LAZYCLOUD_TEST_TOKEN and
LAZYCLOUD_TEST_WORKSPACE naming a stack from `deploy/local/run.sh start`
(server, scheduler and an agent on Docker). A second workspace, named by
LAZYCLOUD_TEST_OTHER_WORKSPACE, must exist and be unreachable from the first.
The scheduler must allow private callback targets.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib
import json
import os
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType

import lazycloud.config
import pytest
from lazycloud.clients.api import ApiClient

from lazycloud import Secret

ENDPOINT = os.environ.get("LAZYCLOUD_TEST_ENDPOINT", "")
TOKEN = os.environ.get("LAZYCLOUD_TEST_TOKEN", "")
WORKSPACE = os.environ.get("LAZYCLOUD_TEST_WORKSPACE", "")
OTHER_WORKSPACE = os.environ.get("LAZYCLOUD_TEST_OTHER_WORKSPACE", "other")

pytestmark = [
    pytest.mark.skipif(not (ENDPOINT and TOKEN and WORKSPACE), reason="needs a running platform"),
    pytest.mark.usefixtures("isolated_imports"),
]

APP = """\
import os
import time

import lazycloud
from lazycloud import Secret

app = lazycloud.App("{app}")
CALLBACK = "{callback}"


def hook(ctx):
    print(f"hook {{ctx.hook.value}} attempt={{ctx.attempt_number}} retry={{ctx.retry_scheduled}}")


@app.function(timeout_seconds=60)
def child(x: int) -> dict:
    task, root = lazycloud.current_task_id(), lazycloud.current_root_task_id()
    return {{"x": x * 2, "task": task, "root": root}}


@app.function(
    secrets=["E2E_TOKEN"],
    timeout_seconds=120,
    on_running=hook,
    on_success=hook,
    on_finish=hook,
    callback_url=CALLBACK,
)
def parent(x: int) -> dict:
    token = os.environ["E2E_TOKEN"]
    print(f"token is {{token}}")
    started = time.monotonic()
    via_api = Secret("E2E_TOKEN").get()
    secret_seconds = time.monotonic() - started
    result = child.remote(x)
    spawned = child.spawn(x + 1)
    try:
        Secret("E2E_TOKEN", workspace="{other}").get()
        trespass = "allowed"
    except Exception as exc:
        trespass = str(exc)
    return {{
        "same": token == via_api,
        "child": result,
        "spawned": spawned.task_id,
        "spawned_value": spawned.get(),
        "me": lazycloud.current_task_id(),
        "trespass": trespass,
        "secret_seconds": secret_seconds,
    }}


@app.function(concurrency=2, in_process=True, timeout_seconds=60)
def together(name: str) -> str:
    print(f"{{name}} start")
    time.sleep(2)
    print(f"{{name}} end")
    return name


@app.function(retries=1, callback_url=CALLBACK, on_error=hook, on_retry=hook, on_failure=hook)
def flaky() -> None:
    raise ValueError("flaky by design")


@app.function(cron="every 1m", timeout_seconds=60)
def tick() -> str:
    return "tick"
"""


class _Callbacks(BaseHTTPRequestHandler):
    received: list[tuple[dict[str, str], bytes]]

    def log_message(self, format: str, *args: object) -> None:
        return None

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.received.append(({k.lower(): v for k, v in self.headers.items()}, body))
        self.send_response(204)
        self.end_headers()


@pytest.fixture
def callbacks() -> Iterator[tuple[str, list[tuple[dict[str, str], bytes]]]]:
    received: list[tuple[dict[str, str], bytes]] = []
    handler = type("_Bound", (_Callbacks,), {"received": received})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/hooks", received
    server.shutdown()
    thread.join()


@pytest.fixture
def platform(monkeypatch: pytest.MonkeyPatch) -> ApiClient:
    monkeypatch.setenv("LAZYCLOUD_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("LAZYCLOUD_TOKEN", TOKEN)
    monkeypatch.setenv("LAZYCLOUD_WORKSPACE", WORKSPACE)
    lazycloud.config.reset_settings_cache()
    return ApiClient(endpoint=ENDPOINT, token=TOKEN, timeout_seconds=60)


def _deploy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, callback: str
) -> tuple[str, ModuleType]:
    app = f"wr{uuid.uuid4().hex[:10]}"
    module = f"{app}_mod"
    (tmp_path / f"{module}.py").write_text(
        APP.format(app=app, callback=callback, other=OTHER_WORKSPACE), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])
    loaded = importlib.import_module(module)
    loaded.app.deploy(source_root=tmp_path)
    return app, loaded


def _logs(client: ApiClient, task_id: str) -> list[str]:
    """The task's output lines; each log entry is one line without its newline."""
    return [e.data for e in client.stream_task_logs(WORKSPACE, uuid.UUID(task_id))]


def _verify(key: str, headers: dict[str, str], body: bytes) -> bool:
    message = base64.b64encode(body).decode() + ":" + headers["x-task-timestamp"]
    digest = hmac.new(key.encode(), message.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, headers["x-task-signature"])


def test_workload_runtime_end_to_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: ApiClient,
    callbacks: tuple[str, list[tuple[dict[str, str], bytes]]],
) -> None:
    url, received = callbacks
    value = f"s3cr3t-{uuid.uuid4().hex}"
    Secret("E2E_TOKEN").set(value)
    app, module = _deploy(tmp_path, monkeypatch, url)

    # The container API: a function reads a secret and spawns another one.
    began = time.monotonic()
    call = module.parent.spawn(3)
    out = call.get(timeout_seconds=180)
    took = time.monotonic() - began
    print(f"parent round trip {took:.2f}s, Secret.get {out['secret_seconds']:.3f}s")
    parent_id = call.task_id
    assert out["same"] is True
    assert out["me"] == parent_id
    assert out["child"]["x"] == 6
    assert out["child"]["root"] == parent_id
    assert out["spawned_value"]["x"] == 8
    assert "cannot reach this workspace" in out["trespass"]
    for child in (out["child"]["task"], out["spawned"]):
        task = platform.get_task(WORKSPACE, uuid.UUID(child))
        assert str(task.parent_task_id) == parent_id
        assert str(task.root_task_id) == parent_id

    # Secrets never reach task output; hooks write to the attempt's log.
    lines = _logs(platform, parent_id)
    assert "token is ********" in lines, lines
    assert all(value not in line for line in lines)
    assert [line for line in lines if line.startswith("hook ")] == [
        "hook on_running attempt=1 retry=False",
        "hook on_success attempt=1 retry=False",
        "hook on_finish attempt=1 retry=False",
    ]

    # in_process: two attempts share one runner process and run together.
    began = time.monotonic()
    first, second = module.together.spawn("a"), module.together.spawn("b")
    assert (first.get(timeout_seconds=120), second.get(timeout_seconds=120)) == ("a", "b")
    print(f"two in-process 2 s attempts finished in {time.monotonic() - began:.2f}s")
    for call, name in ((first, "a"), (second, "b")):
        assert _logs(platform, call.task_id) == [f"{name} start", f"{name} end"]
    a, b = (platform.get_task(WORKSPACE, uuid.UUID(c.task_id)) for c in (first, second))
    assert a.started_at and b.started_at and a.finished_at and b.finished_at
    assert a.started_at < b.finished_at and b.started_at < a.finished_at, "attempts did not overlap"

    # A retried failure runs on_retry, then on_failure; callbacks report both.
    flaky = module.flaky.spawn()
    with pytest.raises(ValueError, match="flaky by design"):
        flaky.get(timeout_seconds=120)
    lines = [line for line in _logs(platform, flaky.task_id) if line.startswith("hook ")]
    assert lines == [
        "hook on_error attempt=1 retry=False",
        "hook on_retry attempt=1 retry=True",
        "hook on_error attempt=2 retry=False",
        "hook on_failure attempt=2 retry=False",
    ]

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and len(received) < 3:
        time.sleep(0.2)
    key = Secret("LAZYCLOUD_CALLBACK_SIGNING_KEY").get()
    events = {}
    for headers, body in received:
        assert _verify(key, headers, body), headers
        payload = json.loads(body)
        assert headers["x-task-id"] == payload["task_id"]
        assert headers["x-task-status"] == payload["status"]
        assert headers["x-task-attempt"] == str(payload["attempt_number"])
        expected = hashlib.sha256(
            f"{payload['task_id']}:{payload['attempt_number']}:{payload['status']}".encode()
        ).hexdigest()
        assert headers["idempotency-key"] == expected
        events[(payload["task_id"], payload["status"])] = payload
    assert events[(parent_id, "succeeded")]["data"]["encoding"] == "cloudpickle"
    assert events[(flaky.task_id, "retry")]["retry_scheduled"] is True
    assert events[(flaky.task_id, "retry")]["error"]["message"] == "flaky by design"
    assert events[(flaky.task_id, "failed")]["attempt_number"] == 2

    # The schedule fires within a minute and records its run.
    function = platform.get_function(WORKSPACE, app, "tick")
    assert function.schedule is not None
    assert (function.schedule.cron, function.schedule.timezone) == ("*/1 * * * *", "UTC")
    deadline = time.monotonic() + 90
    schedule = function.schedule
    while time.monotonic() < deadline and schedule.last_task_id is None:
        time.sleep(1)
        schedule = platform.get_function(WORKSPACE, app, "tick").schedule
        assert schedule is not None
    assert schedule.last_task_id is not None, "the schedule never fired"
    task = platform.get_task(WORKSPACE, schedule.last_task_id, wait_seconds=60)
    assert task.status.value == "succeeded"
    assert task.scheduled_for == schedule.last_run_at
    listed = platform.list_schedules(WORKSPACE)
    assert any(item.app == app and item.function == "tick" for item in listed.schedules)
