"""Apps, deployments, tasks, logs and containers through the CLI and SDK handles."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from lazycloud.cli.components.errors import ClientError
from lazycloud.cli.main import build_public_cli
from lazycloud.exceptions import UnsupportedFeatureError
from lazycloud.session.deployment import DeploymentClient
from typer.testing import CliRunner, Result

import lazycloud
from tests.api_server import ApiRequest, FakeApi, Reply, error_reply, json_reply

pytestmark = pytest.mark.usefixtures("isolated_imports")

NOW = "2026-09-30T12:00:00Z"
TEAM = "/v1/workspaces/team"
APP_ID = "0192f0a0-0000-7000-8000-0000000000a1"
WORKLOAD_ID = "0192f0a0-0000-7000-8000-0000000000b1"
TASK_ID = "0192f0a0-0000-7000-8000-0000000000c1"
CONTAINER_ID = "0192f0a0-0000-7000-8000-0000000000d1"
RELEASE_ID = "0192f0a0-0000-7000-8000-0000000000e1"

REPORTS = """\
import lazycloud

app = lazycloud.App("reports")


@app.function()
def summarize_sales(values: list[int]) -> int:
    return sum(values)


@app.function()
def forecast(months: int) -> int:
    return months
"""


def _cli(*args: str) -> Result:
    return CliRunner().invoke(build_public_cli(), list(args))


def _app(state: str = "active") -> dict[str, object]:
    return {"id": APP_ID, "name": "reports", "state": state, "workloads": 2, "created_at": NOW}


def _deployment(name: str = "summarize_sales", **extra: object) -> dict[str, object]:
    return {
        "id": WORKLOAD_ID,
        "app": "reports",
        "name": name,
        "kind": "function",
        "state": "active",
        "version": 3,
        "release_id": RELEASE_ID,
        "created_at": NOW,
        **extra,
    }


def _task(status: str = "running", **extra: object) -> dict[str, object]:
    return {
        "id": TASK_ID,
        "app": "reports",
        "function": "summarize_sales",
        "release_id": RELEASE_ID,
        "root_task_id": TASK_ID,
        "status": status,
        "attempts": 2,
        "max_attempts": 3,
        "created_at": NOW,
        **extra,
    }


def _entry(number: int, data: str, stream: str = "stdout") -> bytes:
    record: dict[str, object] = {
        "id": number,
        "task_id": TASK_ID,
        "attempt": 1,
        "stream": stream,
        "data": data,
        "time": NOW,
    }
    return json.dumps(record).encode() + b"\n"


def _container(state: str = "stopped", **extra: object) -> dict[str, object]:
    return {
        "id": CONTAINER_ID,
        "app": "reports",
        "function": "summarize_sales",
        "release_id": RELEASE_ID,
        "version": 3,
        "state": state,
        "slots": 1,
        "running_tasks": 0,
        "cpu_millis": 250,
        "memory_mib": 512,
        "created_at": NOW,
        **extra,
    }


def test_app_commands_accept_a_name_and_report_each_outcome(fake_api: FakeApi) -> None:
    fake_api.route("GET", f"{TEAM}/apps")(lambda _: json_reply({"apps": [_app("paused")]}))
    fake_api.route("POST", f"{TEAM}/apps/reports/pause")(lambda _: json_reply(_app("paused")))
    fake_api.route("POST", f"{TEAM}/apps/reports/resume")(lambda _: json_reply(_app()))
    fake_api.route("DELETE", f"{TEAM}/apps/reports")(lambda _: json_reply(_app("deleted")))

    listed = _cli("app", "list", "--inactive")
    paused = _cli("app", "pause", "reports")
    resumed = _cli("app", "resume", "reports")
    deleted = _cli("--json", "app", "delete", "reports")
    conflicting = _cli("app", "list", "--active", "--all")

    assert listed.exit_code == 0, listed.output
    assert fake_api.calls("GET", f"{TEAM}/apps")[0].query["state"] == ["paused"]
    assert "reports" in listed.stdout and "paused" in listed.stdout
    assert "Paused reports." in paused.stdout
    assert "Resumed reports." in resumed.stdout
    assert json.loads(deleted.stdout) == {"app_id": APP_ID, "deleted": True}
    assert conflicting.exit_code != 0


def test_deployment_references_resolve_names_and_versions(fake_api: FakeApi) -> None:
    @fake_api.route("GET", f"{TEAM}/deployments")
    def deployments(request: ApiRequest) -> Reply:
        name = request.query.get("name", [""])[0]
        matches = {
            "summarize_sales": [_deployment()],
            "shared": [_deployment("shared", app="reports"), _deployment("shared", app="billing")],
        }
        return json_reply({"deployments": matches.get(name, [])})

    started = f"{TEAM}/deployments/{WORKLOAD_ID}/start"
    fake_api.route("POST", started)(lambda _: json_reply(_deployment()))
    fake_api.route("DELETE", f"{TEAM}/deployments/{WORKLOAD_ID}")(
        lambda _: json_reply(_deployment(state="deleted"))
    )

    start = _cli("deployment", "start", "summarize_sales-v2")
    delete = _cli("--json", "deployment", "delete", "summarize_sales")
    ambiguous = _cli("deployment", "stop", "shared")
    missing = _cli("deployment", "stop", "absent")
    one_version = _cli("deployment", "delete", "summarize_sales-v2")

    assert start.exit_code == 0, start.output
    assert "Started summarize_sales." in start.stdout
    assert fake_api.calls("POST", started)[0].json() == {"version": 2}
    assert json.loads(delete.stdout) == {"deployment_id": "summarize_sales", "deleted": True}
    assert "billing, reports" in str(ambiguous.exception)
    assert "deployment not found: absent" in str(missing.exception)
    assert one_version.exit_code != 0
    assert len(fake_api.calls("DELETE", f"{TEAM}/deployments/{WORKLOAD_ID}")) == 1


def test_deploy_diff_previews_the_plan_without_deploying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_api: FakeApi
) -> None:
    (tmp_path / "reports.py").write_text(REPORTS, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])

    @fake_api.route("POST", f"{TEAM}/apps/reports/deployment-plan")
    def plan(request: ApiRequest) -> Reply:
        listed: list[dict[str, object]] = [
            {"kind": "function", "name": item["name"], "action": "add", "versions": 0}
            for item in request.json()["workloads"]
        ]
        removed: dict[str, object] = {
            "kind": "function",
            "name": "old_job",
            "action": "remove",
            "versions": 4,
        }
        return json_reply({"app": "reports", "prune": True, "items": [*listed, removed]})

    result = _cli("deploy", "reports.py", "--diff", "--prune")

    assert result.exit_code == 0, result.output
    (request,) = fake_api.calls("POST", f"{TEAM}/apps/reports/deployment-plan")
    assert request.json() == {
        "workloads": [
            {"kind": "function", "name": "summarize_sales"},
            {"kind": "function", "name": "forecast"},
        ],
        "prune": True,
    }
    assert "old_job" in result.stdout and "remove" in result.stdout
    assert fake_api.calls("POST", f"{TEAM}/apps/reports/deployments") == []


def test_task_commands_list_show_stop_and_report_failures(fake_api: FakeApi) -> None:
    failure = {"kind": "user_error", "type": "ValueError", "message": "no sales"}
    fake_api.route("GET", f"{TEAM}/tasks")(lambda _: json_reply({"tasks": [_task()]}))
    fake_api.route("GET", f"{TEAM}/tasks/{TASK_ID}")(
        lambda _: json_reply(_task("failed", container_id=CONTAINER_ID, failure=failure))
    )
    fake_api.route("POST", f"{TEAM}/tasks/stop")(
        lambda _: json_reply({"stopped": [], "skipped": [TASK_ID]})
    )
    fake_api.route("GET", f"{TEAM}/tasks/{TASK_ID}/logs")(
        lambda _: (200, {}, _entry(4, "line four") + _entry(5, "line five"))
    )

    listed = _cli("task", "list", "--app", "reports")
    shown = _cli("task", "show", TASK_ID)
    stopped = _cli("task", "stop", TASK_ID)
    logs = _cli("task", "logs", TASK_ID, "--limit", "2")
    result = _cli("task", "result", TASK_ID)

    assert listed.exit_code == 0, listed.output
    assert fake_api.calls("GET", f"{TEAM}/tasks")[0].query["app"] == ["reports"]
    assert "summarize_sales" in listed.stdout
    assert "2 of 3" in shown.stdout and "ValueError: no sales" in shown.stdout
    assert "Tasks stopped" in stopped.stdout and TASK_ID in stopped.stdout
    assert fake_api.calls("POST", f"{TEAM}/tasks/stop")[0].json() == {"task_ids": [TASK_ID]}
    assert logs.stdout == "line four\nline five\n"
    assert fake_api.calls("GET", f"{TEAM}/tasks/{TASK_ID}/logs")[0].query["tail"] == ["2"]
    assert isinstance(result.exception, ClientError)
    assert result.exception.details.type == "task_failed"
    assert result.exception.details.message == "ValueError: no sales"


def test_logs_needs_one_source_and_follows_as_ndjson(fake_api: FakeApi) -> None:
    fake_api.route("GET", f"{TEAM}/tasks/{TASK_ID}/logs")(
        lambda _: (200, {}, _entry(1, "first") + b"\n" + _entry(2, "second", "stderr"))
    )

    followed = _cli("--json", "logs", "--task-id", TASK_ID, "--follow", "-n", "10")
    stamped = _cli("logs", "--task-id", TASK_ID, "--show-timestamp")
    limited = _cli("logs", "--task-id", TASK_ID, "-f", "--max-events", "1")
    both = _cli("logs", "--task-id", TASK_ID, "--container-id", CONTAINER_ID)

    assert followed.exit_code == 0, followed.output
    lines = [json.loads(line) for line in followed.stdout.splitlines()]
    assert [line["data"] for line in lines] == ["first", "second"]
    first_read = fake_api.calls("GET", f"{TEAM}/tasks/{TASK_ID}/logs")[0]
    assert (first_read.query["tail"], first_read.query["follow"]) == (["10"], ["true"])
    assert stamped.stdout.splitlines()[0] == "[2026-09-30T12:00:00+00:00] first"
    assert limited.stdout == "first\n"
    assert both.exit_code != 0


def test_container_commands_list_stop_and_attach_until_it_stops(fake_api: FakeApi) -> None:
    fake_api.route("GET", f"{TEAM}/containers")(
        lambda _: json_reply({"containers": [_container(stop_reason="out_of_memory")]})
    )
    fake_api.route("POST", f"{TEAM}/containers/{CONTAINER_ID}/stop")(
        lambda _: json_reply(_container("draining"))
    )
    fake_api.route("GET", f"{TEAM}/containers/{CONTAINER_ID}/logs")(
        lambda _: (200, {}, _entry(1, "loading model"))
    )
    fake_api.route("GET", f"{TEAM}/containers/{CONTAINER_ID}")(
        lambda _: json_reply(_container(stop_reason="crashed"))
    )

    listed = _cli("container", "list")
    stopped = _cli("container", "stop", CONTAINER_ID)
    attached = _cli("container", "attach", CONTAINER_ID)
    invalid = _cli("container", "stop", "not-an-id")

    assert listed.exit_code == 0, listed.output
    assert "out_of_memory" in listed.stdout and "summarize_sales" in listed.stdout
    assert "Stopped 1 container." in stopped.stdout
    assert attached.stdout.startswith("loading model\n")
    assert "Container finished" in attached.stdout and "crashed" in attached.stdout
    assert attached.exit_code == 1
    assert invalid.exit_code != 0


def test_unknown_task_ids_are_not_found(fake_api: FakeApi) -> None:
    fake_api.route("GET", f"{TEAM}/tasks/{TASK_ID}")(
        lambda _: error_reply("not_found", "no such task", 404)
    )

    result = _cli("task", "show", TASK_ID)

    assert result.exit_code != 0
    assert f"task not found: {TASK_ID}" in str(result.exception)


def test_deployment_handles_submit_to_the_active_version(fake_api: FakeApi) -> None:
    fake_api.route("GET", f"{TEAM}/deployments")(
        lambda _: json_reply({"deployments": [_deployment()]})
    )
    submitted = f"{TEAM}/apps/reports/functions/summarize_sales/tasks"
    fake_api.route("POST", submitted)(lambda _: json_reply({"tasks": [_task("queued")]}, 201))
    fake_api.route("GET", f"{TEAM}/tasks/{TASK_ID}")(lambda _: json_reply(_task("succeeded")))
    fake_api.route("GET", f"{TEAM}/tasks/{TASK_ID}/result")(
        lambda _: json_reply({"encoding": "json", "value": 12})
    )

    deployment = DeploymentClient().handle("summarize_sales")
    submission = deployment.submit([5, 7])

    assert isinstance(deployment, lazycloud.Deployment)
    assert (deployment.id, deployment.stub_id) == (WORKLOAD_ID, RELEASE_ID)
    assert submission.task_id == TASK_ID
    assert submission.result(wait=True) == 12
    (request,) = fake_api.calls("POST", submitted)
    assert "release_id" not in request.json()
    with pytest.raises(UnsupportedFeatureError):
        deployment.invoke_url()
