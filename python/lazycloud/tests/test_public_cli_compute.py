"""`lazycloud compute` and `lazycloud machine` on the public API's compute operations."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest
from lazycloud._terminal.streams import console
from lazycloud.cli.main import build_public_cli
from typer.testing import CliRunner

from tests.api_server import ApiRequest, FakeApi, Reply, error_reply, json_reply

NOW = "2026-09-30T12:00:00Z"
MACHINE_ID = "0192f0a0-0000-7000-8000-0000000000aa"

cli = build_public_cli()


@pytest.fixture(autouse=True)
def wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tables keep every column on one line."""
    monkeypatch.setattr(console, "width", 200)


def _machine(name: str, *, index: int = 0, **extra: object) -> dict[str, object]:
    return {
        "id": f"0192f0a0-0000-7000-8000-{index:012d}",
        "name": name,
        "workspaces": ["team"],
        "placement": f"machine:0192f0a0-0000-7000-8000-{index:012d}",
        "provider": "agent",
        "lifecycle": "ready",
        "lifecycle_message": "",
        "lifecycle_at": NOW,
        "cpu": 8000,
        "memory": 32768,
        "gpu": "",
        "gpu_count": 0,
        "connected": True,
        "schedulable": True,
        "capacity_state": "available",
        "capacity_reason": "",
        "preflight_checks": [],
        "remediation": [],
        "agent_version": "1.0.0",
        "created_at": NOW,
        "updated_at": NOW,
        **extra,
    }


def _instance(index: int, **extra: object) -> dict[str, object]:
    return {
        "id": f"0192f0a0-0000-7000-8000-{index:012d}",
        "placement": "connection:0192f0a0-0000-7000-8000-0000000000c1",
        "provider": "aws",
        "region": "us-east-2",
        "availability_zone": "us-east-2a",
        "instance_id": f"i-{index:017x}",
        "instance_type": "g6.xlarge",
        "lifecycle": "ready",
        "lifecycle_message": "",
        "lifecycle_at": NOW,
        "connected": True,
        "capacity_state": "available",
        "capacity_reason": "",
        "gpu_count": 1,
        "cpu_millicores": 2000,
        "memory_mb": 16384,
        "launch_attempt": 1,
        "booted_template_version": "1.0.0",
        "created_at": NOW,
        **extra,
    }


def _paged(key: str, items: list[dict[str, object]]):
    """A route serving `items` two per page by index cursor."""

    def route(request: ApiRequest) -> Reply:
        assert request.query["limit"] == ["100"]
        start = int(request.query.get("cursor", ["0"])[0])
        payload: dict[str, object] = {key: items[start : start + 2]}
        if start + 2 < len(items):
            payload["next_cursor"] = str(start + 2)
        return json_reply(payload)

    return route


def test_compute_status_summarizes_the_workspace(fake_api: FakeApi) -> None:
    summary: dict[str, object] = {
        "connection": {"account_id": "123456789012", "phase": "ready"},
        "instances": {"total": 3, "ready": 1, "pending": 2, "degraded": 0},
        "cost": {"hourly_micros": 340000, "currency": "USD", "estimated": True},
        "workload_count": 3,
    }
    fake_api.route("GET", "/v1/workspaces/platform/compute")(lambda _: json_reply(summary))

    shown = CliRunner().invoke(cli, ["compute", "status", "--workspace", "platform"])
    assert shown.exit_code == 0, shown.output
    assert "123456789012" in shown.stdout
    assert "1 ready, 2 pending" in shown.stdout
    assert "degraded" not in shown.stdout

    payload = json.loads(
        CliRunner().invoke(cli, ["--json", "compute", "status", "--workspace", "platform"]).stdout
    )
    assert payload["connection"] == {"account_id": "123456789012", "phase": "ready"}
    assert payload["instances"] == {"total": 3, "ready": 1, "pending": 2, "degraded": 0}
    assert payload["workload_count"] == 3

    summary["connection"] = None
    summary["instances"] = {"total": 1, "ready": 0, "pending": 0, "degraded": 1}
    platform = CliRunner().invoke(cli, ["compute", "status", "--workspace", "platform"])
    assert "not connected" in platform.stdout
    assert "0 ready, 1 degraded" in platform.stdout


def test_compute_instances_and_workloads_follow_every_page(fake_api: FakeApi) -> None:
    instances = [
        _instance(1),
        _instance(2, lifecycle="failed", lifecycle_failure="bootstrap_timed_out"),
        _instance(3, lifecycle="booting", lifecycle_message="Waiting for the agent"),
    ]
    fake_api.route("GET", "/v1/compute/instances")(_paged("instances", instances))
    workloads: list[dict[str, object]] = [
        {
            "deployment_id": f"0192f0a0-0000-7000-8000-{index:012d}",
            "app": "reports",
            "name": name,
            "kind": "function",
            "machine": machine,
            "cpu_millicores": 1000,
            "memory_mb": 1024,
            "gpu": [],
            "gpu_count": 0,
        }
        for index, (name, machine) in enumerate([("train", "gpu-1"), ("serve", ""), ("etl", "")])
    ]
    fake_api.route("GET", "/v1/workspaces/team/compute/workloads")(_paged("workloads", workloads))

    listed = CliRunner().invoke(cli, ["compute", "instances"])
    assert listed.exit_code == 0, listed.output
    for value in ("g6.xlarge", "bootstrap_timed_out", "Waiting for the agent", "booting"):
        assert value in listed.stdout
    payload = json.loads(CliRunner().invoke(cli, ["--json", "compute", "instances"]).stdout)
    assert [item["instance_id"] for item in payload["instances"]] == [
        item["instance_id"] for item in instances
    ]

    shown = CliRunner().invoke(cli, ["compute", "workloads"])
    assert shown.exit_code == 0, shown.output
    assert "train" in shown.stdout and "gpu-1" in shown.stdout and "etl" in shown.stdout
    payload = json.loads(CliRunner().invoke(cli, ["--json", "compute", "workloads"]).stdout)
    assert [item["name"] for item in payload["workloads"]] == ["train", "serve", "etl"]

    fake_api.route("GET", "/v1/compute/instances")(lambda _: json_reply({"instances": []}))
    empty = CliRunner().invoke(cli, ["compute", "instances"])
    assert "No compute instances found." in empty.stdout


def test_machine_list_update_and_remove(fake_api: FakeApi) -> None:
    machines = [
        _machine("gpu-1", index=1, gpu="L4", gpu_count=1, workspaces=["team", "dev"]),
        _machine("gpu-2", index=2, lifecycle="joining"),
        _machine("cpu-1", index=3, lifecycle="failed"),
    ]
    fake_api.route("GET", "/v1/workspaces/team/machines")(_paged("machines", machines))

    @fake_api.route("PATCH", "/v1/machines/[a-z0-9-]+")
    def update(request: ApiRequest) -> Reply:
        name = request.path.rsplit("/", 1)[1]
        return json_reply(_machine(name, workspaces=request.json()["workspaces"]))

    @fake_api.route("DELETE", "/v1/machines/[a-z0-9-]+")
    def remove(request: ApiRequest) -> Reply:
        if request.path.endswith("/missing"):
            return error_reply("not_found", "machine not found: missing", 404)
        return 204, {}, b""

    listed = CliRunner().invoke(cli, ["machine", "list"])
    assert listed.exit_code == 0, listed.output
    for value in ("gpu-1", "team, dev", "L4", "joining", "cpu-1", "failed"):
        assert value in listed.stdout
    rows = json.loads(CliRunner().invoke(cli, ["--json", "machine", "list"]).stdout)
    assert [row["name"] for row in rows] == ["gpu-1", "gpu-2", "cpu-1"]

    updated = CliRunner().invoke(
        cli,
        [
            "--json",
            "machine",
            "update",
            "gpu-1",
            "--workspaces",
            "dev, team",
            "--workspaces",
            "dev",
        ],
    )
    assert updated.exit_code == 0, updated.output
    (patch,) = fake_api.calls("PATCH", "/v1/machines/gpu-1")
    assert patch.json() == {"workspaces": ["dev", "team"]}
    assert json.loads(updated.stdout)["workspaces"] == ["dev", "team"]
    card = CliRunner().invoke(cli, ["machine", "update", "gpu-1", "--workspaces", "dev"])
    assert "gpu-1" in card.stdout and "dev" in card.stdout
    blank = CliRunner().invoke(cli, ["machine", "update", "gpu-1", "--workspaces", " , "])
    assert blank.exit_code == 2
    assert len(fake_api.calls("PATCH", "/v1/machines/gpu-1")) == 2

    removed = CliRunner().invoke(cli, ["--json", "machine", "remove", MACHINE_ID])
    assert removed.exit_code == 0, removed.output
    assert json.loads(removed.stdout) == {"machine_id": MACHINE_ID, "removed": True}
    assert fake_api.calls("DELETE", f"/v1/machines/{MACHINE_ID}")
    assert "Removed gpu-2." in CliRunner().invoke(cli, ["machine", "remove", "gpu-2"]).stdout
    assert "machine not found" in str(
        CliRunner().invoke(cli, ["machine", "remove", "missing"]).exception
    )


def _serve_join(api: FakeApi, command: str) -> None:
    @api.route("POST", "/v1/machines/join-command")
    def join(request: ApiRequest) -> Reply:
        body = request.json()
        machine = _machine(body["name"], workspaces=body["workspaces"], lifecycle="requested")
        return json_reply({"command": command, "expires_at": NOW, "machine": machine}, 201)


def test_machine_join_runs_the_command_with_the_agent_flags(
    fake_api: FakeApi, tmp_path: Path
) -> None:
    recorded = tmp_path / "args"
    # The server's command, standing in for the installer: it records the
    # flags the CLI appends.
    _serve_join(fake_api, f'sh -c \'printf "%s\\n" "$@" > {shlex.quote(str(recorded))}\' join')

    joined = CliRunner().invoke(
        cli,
        [
            "machine",
            "join",
            "--name",
            "gpu-1",
            "--workspaces",
            "team,dev",
            "--gpu",
            "L4",
            "--max-cpu",
            "4",
            "--max-memory",
            "16Gi",
            "--max-gpus",
            "1",
            "--service-name",
            "lazycloud agent",
            "--state-dir",
            str(tmp_path / "state dir"),
        ],
    )

    assert joined.exit_code == 0, joined.output
    assert "The agent is running." in joined.stdout
    assert "Machine joined" in joined.stdout
    (request,) = fake_api.calls("POST", "/v1/machines/join-command")
    assert request.json() == {"name": "gpu-1", "workspaces": ["team", "dev"], "gpu": ["L4"]}
    assert recorded.read_text().splitlines() == [
        "--foreground",
        "--service-name",
        "lazycloud agent",
        "--state-dir",
        str(tmp_path / "state dir"),
        "--max-cpu",
        "4",
        "--max-memory",
        "16Gi",
        "--max-gpus",
        "1",
    ]


def test_machine_join_background_json_and_exit_codes(
    fake_api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve_join(fake_api, "curl -fsSL https://api.example.com/install/agent | sh -s -- join TOKEN")
    calls: list[tuple[str, object]] = []
    outcome: list[int | BaseException] = [0]

    def call(command: str, *, shell: bool, stdout: object) -> int:
        assert shell is True
        calls.append((command, stdout))
        result = outcome[0]
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr("lazycloud.cli.resources.subprocess.call", call)
    args = [
        "machine",
        "join",
        "--name",
        "gpu-1",
        "--workspaces",
        "team",
        "--background",
        "--service-manager",
        "systemd",
        "--gpu-ids",
        "0,1",
    ]

    joined = CliRunner().invoke(cli, ["--json", *args])
    assert joined.exit_code == 0, joined.output
    assert json.loads(joined.stdout) == {"status": "running"}
    command, stdout = calls[-1]
    assert command == (
        "curl -fsSL https://api.example.com/install/agent | sh -s -- join TOKEN"
        " --background --service-manager systemd --gpu-ids 0,1"
    )
    # Under --json the installer's output goes to stderr so stdout stays one document.
    assert stdout is not None

    CliRunner().invoke(cli, args)
    assert calls[-1][1] is None

    for interrupted in (130, -2, KeyboardInterrupt()):
        outcome[0] = interrupted
        result = CliRunner().invoke(cli, args)
        assert result.exit_code == 0, result.output
        assert result.stdout == ""

    outcome[0] = 3
    failed = CliRunner().invoke(cli, args)
    assert failed.exit_code == 3
    assert "Machine joined" not in failed.stdout

    before = len(calls)
    both = CliRunner().invoke(cli, [*args, "--max-gpus", "1"])
    assert both.exit_code == 2
    assert "--gpu-ids and --max-gpus cannot both be set" in both.output
    assert len(calls) == before
