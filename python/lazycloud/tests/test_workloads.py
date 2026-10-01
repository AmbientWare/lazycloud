"""Pods, devboxes and sandboxes against a fake API: the requests they send and what they print."""

from __future__ import annotations

import hashlib
import importlib
import io
import json
import sys
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from lazycloud.abstractions.sandbox import (
    SandboxConnectionError,
    SandboxFileSystemError,
    SandboxInstance,
)
from lazycloud.cli.main import build_public_cli
from lazycloud.clients.api import ApiClient
from lazycloud.clients.workloads import WorkloadsClient
from typer.testing import CliRunner, Result

from tests.api_server import TOKEN, ApiRequest, FakeApi, Reply, error_reply, json_reply

pytestmark = pytest.mark.usefixtures("isolated_imports")

NOW = "2026-10-01T12:00:00Z"
IMAGE_ID = "img_0123456789abcdef01234567"
RELEASE = "0192f0a0-0000-7000-8000-0000000000a1"
CONTAINER = "0192f0a0-0000-7000-8000-0000000000c1"
DEPLOYMENT = "0192f0a0-0000-7000-8000-0000000000d1"
SNAPSHOT = "0192f0a0-0000-7000-8000-0000000000e1"
HOST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
TEAM = "/v1/workspaces/team"
BOX = f"{TEAM}/containers/{CONTAINER}"

TOOLS = """\
import lazycloud

app = lazycloud.App("tools")

web = app.pod(
    name="web",
    command=["python", "-m", "http.server", "8080"],
    ports={"http": 8080},
    cpu=0.5,
    memory="256Mi",
    env={"MODE": "test"},
    keep_warm=-1,
    health_check_path="/health",
    ssh=True,
    allow_list=["10.0.0.0/8"],
    docker_enabled=True,
    preemptible=False,
    checkpoint_enabled=True,
    checkpoint_readiness_path="/ready",
    checkpoint_readiness_port=8080,
)

box = app.devbox(
    "box", image=lazycloud.Image(), disk="10Gi", cpu=2, memory="4Gi", agent_harnesses=[]
)

scratch = app.sandbox(name="scratch", ports=[8000], block_network=True)
"""


def _project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    (tmp_path / "tools.py").write_text(TOOLS, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])
    sys.modules.pop("tools", None)
    return importlib.import_module("tools")


def _cli(*args: str) -> Result:
    return CliRunner().invoke(build_public_cli(), list(args))


def _release(spec: dict[str, Any], **extra: object) -> dict[str, object]:
    return {"id": RELEASE, "function": spec["name"], "created_at": NOW, "spec": spec, **extra}


def _instance(state: str = "pending", **extra: object) -> dict[str, object]:
    return {
        "id": CONTAINER,
        "release_id": RELEASE,
        "app": "tools",
        "name": "scratch",
        "kind": "sandbox",
        "state": state,
        "created_at": NOW,
        **extra,
    }


def _container(**extra: object) -> dict[str, object]:
    return {
        "id": CONTAINER,
        "app": "tools",
        "function": "web",
        "release_id": RELEASE,
        "state": "ready",
        "slots": 1,
        "running_tasks": 0,
        "cpu_millis": 500,
        "memory_mib": 256,
        "created_at": NOW,
        "kind": "pod",
        **extra,
    }


def _deployed(name: str = "web", **extra: object) -> dict[str, object]:
    return {
        "id": DEPLOYMENT,
        "app": "tools",
        "name": name,
        "kind": "pod",
        "state": "active",
        "version": 2,
        "release_id": RELEASE,
        "created_at": NOW,
        **extra,
    }


def _no_content(_: ApiRequest) -> Reply:
    return 204, {}, b""


def _serve_releases(api: FakeApi, stored: set[str]) -> None:
    api.route("POST", f"{TEAM}/images/resolve")(
        lambda request: json_reply(
            {
                "image": {
                    "id": IMAGE_ID,
                    "python_version": request.json()["python_version"],
                    "architecture": "amd64",
                    "ready": True,
                    "created_at": NOW,
                }
            }
        )
    )

    @api.route("POST", f"{TEAM}/sources")
    def sources(request: ApiRequest) -> Reply:
        sha: str = request.json()["sha256"]
        state: dict[str, object] = {"sha256": sha, "present": sha in stored}
        if sha not in stored:
            state["upload"] = {
                "url": f"{api.url}/upload/{sha}",
                "method": "PUT",
                "headers": {},
                "expires_at": NOW,
            }
        return json_reply(state)

    @api.route("PUT", "/upload/[0-9a-f]{64}")
    def upload(request: ApiRequest) -> Reply:
        stored.add(request.path.rsplit("/", 1)[1])
        return 200, {}, b""

    @api.route("POST", f"{TEAM}/apps/tools/deployments")
    def deploy(request: ApiRequest) -> Reply:
        functions: list[dict[str, Any]] = request.json()["functions"]
        return json_reply(
            {
                "app": {
                    "id": DEPLOYMENT,
                    "name": "tools",
                    "state": "active",
                    "workloads": len(functions),
                    "created_at": NOW,
                },
                "releases": [
                    _release(spec, version=1, url=f"https://{spec['name']}.example.test")
                    for spec in functions
                ],
                "pruned": [],
                "removed_versions": 0,
            }
        )

    api.route("POST", f"{TEAM}/apps/tools/functions/[^/]+/releases")(
        lambda request: json_reply(_release(request.json()))
    )


def test_app_deploy_sends_pods_and_devboxes_with_their_defaults_and_never_sandboxes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_api: FakeApi
) -> None:
    _project(tmp_path, monkeypatch)
    _serve_releases(fake_api, set())

    result = _cli("deploy", "tools.py")

    assert result.exit_code == 0, result.output
    (request,) = fake_api.calls("POST", f"{TEAM}/apps/tools/deployments")
    web, box = request.json()["functions"]
    assert "handler" not in web
    assert web["pod"] == {
        "kind": "pod",
        "command": ["python", "-m", "http.server", "8080"],
        "ports": {"http": 8080},
        "ssh": True,
        "health_check": {"path": "/health"},
        "allow_list": ["10.0.0.0/8"],
    }
    assert web["resources"] == {"cpu_millis": 500, "memory_mib": 256}
    assert web["keep_warm_seconds"] == -1
    assert web["environment"] == {"MODE": "test"}
    assert web["authorized"] is False
    assert web["docker_enabled"] is True
    assert web["placement"] == {"preemptible": False}
    assert web["checkpoint"] == {
        "readiness_path": "/ready",
        "readiness_port": 8080,
        "readiness_timeout_seconds": 600,
        "readiness_interval_seconds": 1.0,
    }
    assert box["pod"] == {"kind": "devbox"}
    assert box["keep_warm_seconds"] == 1800
    assert box["placement"] == {"preemptible": False}
    assert box["disks"] == [{"name": "box", "size_bytes": 10 * 1024**3, "mount_path": "/"}]
    assert web["source"] == box["source"]
    assert "Devboxes" in result.output
    assert "box" in result.output


def test_single_pod_deploy_card_shows_role_keep_warm_and_preemptible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_api: FakeApi
) -> None:
    _project(tmp_path, monkeypatch)
    _serve_releases(fake_api, set())

    result = _cli("--json", "deploy", "tools:web")
    card = _cli("deploy", "tools:web")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["releases"][0]["spec"]["pod"]["kind"] == "pod"
    assert card.exit_code == 0, card.output
    for text in ("Deployment created", "pod", "always", "https://web.example.test"):
        assert text in card.output


def test_pod_instances_start_from_the_prepared_release_and_terminate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_api: FakeApi
) -> None:
    tools = _project(tmp_path, monkeypatch)
    _serve_releases(fake_api, set())
    fake_api.route("POST", f"{TEAM}/instances")(
        lambda request: json_reply(
            _instance(
                name="web",
                kind="pod",
                url="https://instance.example.test",
                timeout_seconds=request.json().get("timeout_seconds"),
                expires_at=NOW,
            ),
            201,
        )
    )
    fake_api.route("POST", f"{BOX}/stop")(
        lambda request: json_reply(_container(state="stopped", stop_reason="stopped"))
    )

    instance = tools.web.create(timeout_seconds=60)
    stopped = instance.terminate()
    run = _cli("run", "tools:web", "echo", "hi")

    assert (instance.container_id, instance.stub_id) == (CONTAINER, RELEASE)
    assert (instance.url, instance.timeout_seconds) == ("https://instance.example.test", 60)
    assert stopped is True
    first, second = fake_api.calls("POST", f"{TEAM}/instances")
    assert first.json() == {"release_id": RELEASE, "timeout_seconds": 60}
    assert second.json() == {
        "release_id": RELEASE,
        "command": ["echo", "hi"],
        "timeout_seconds": -1,
    }
    assert run.exit_code == 0, run.output
    assert CONTAINER in run.output
    # The prepared release is reused for later instances from this process.
    assert len(fake_api.calls("POST", f"{TEAM}/apps/tools/functions/web/releases")) == 1


def test_pod_lifecycle_resolves_its_deployment_by_app_and_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_api: FakeApi
) -> None:
    tools = _project(tmp_path, monkeypatch)

    @fake_api.route("GET", f"{TEAM}/deployments")
    def deployments(request: ApiRequest) -> Reply:
        named = request.query.get("name") == ["web"]
        return json_reply({"deployments": [_deployed()] if named else []})

    for action in ("stop", "start", "scale"):
        fake_api.route("POST", f"{TEAM}/deployments/{DEPLOYMENT}/{action}")(
            lambda request: json_reply(
                _deployed(scaling={"min_containers": 2, "max_containers": 2})
            )
        )
    fake_api.route("DELETE", f"{TEAM}/deployments/{DEPLOYMENT}")(
        lambda request: json_reply(_deployed(state="deleted"))
    )

    tools.web.scale(2)
    tools.web.pause()
    tools.web.resume(version=1)
    tools.web.delete()
    scaled = _cli("deployment", "scale", "web", "--containers", "2")

    assert {tuple(r.query["app"]) for r in fake_api.calls("GET", f"{TEAM}/deployments")[:4]} == {
        ("tools",)
    }
    assert [r.json() for r in fake_api.calls("POST", f"{TEAM}/deployments/{DEPLOYMENT}/scale")] == [
        {"containers": 2},
        {"containers": 2},
    ]
    assert fake_api.calls("POST", f"{TEAM}/deployments/{DEPLOYMENT}/start")[0].json() == {
        "version": 1
    }
    assert len(fake_api.calls("DELETE", f"{TEAM}/deployments/{DEPLOYMENT}")) == 1
    assert scaled.exit_code == 0, scaled.output
    assert "Set web to 2 containers." in scaled.output


def test_sandbox_create_prepares_an_empty_workspace_waits_and_exposes_its_ports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_api: FakeApi
) -> None:
    tools = _project(tmp_path, monkeypatch)
    stored: set[str] = set()
    _serve_releases(fake_api, stored)
    fake_api.route("POST", f"{TEAM}/instances")(lambda request: json_reply(_instance(), 201))
    connects: list[int] = []

    @fake_api.route("POST", f"{BOX}/connect")
    def connect(request: ApiRequest) -> Reply:
        connects.append(1)
        if len(connects) == 1:
            return error_reply("unavailable", "pulling image", 503)
        return json_reply(_instance("ready"))

    fake_api.route("POST", f"{BOX}/ports")(
        lambda request: json_reply({"port": 8000, "url": "https://p.example.test"})
    )

    instance = tools.scratch.create()

    empty = io.BytesIO()
    zipfile.ZipFile(empty, "w").close()
    (release,) = fake_api.calls("POST", f"{TEAM}/apps/tools/functions/scratch/releases")
    spec = release.json()
    assert spec["source"] == {"sha256": hashlib.sha256(empty.getvalue()).hexdigest()}
    assert spec["pod"] == {
        "kind": "sandbox",
        "command": ["tail", "-f", "/dev/null"],
        "ports": {"8000": 8000},
        "block_network": True,
    }
    assert spec["keep_warm_seconds"] == 600
    assert fake_api.calls("POST", f"{TEAM}/instances")[0].json() == {"release_id": RELEASE}
    assert len(connects) == 2
    assert fake_api.calls("POST", f"{BOX}/ports")[0].json() == {"port": 8000}
    assert (instance.id, instance.stub_id) == (CONTAINER, RELEASE)


def test_a_sandbox_that_stops_while_starting_is_terminated_and_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_api: FakeApi
) -> None:
    tools = _project(tmp_path, monkeypatch)
    tools.scratch.stub_id = RELEASE
    fake_api.route("POST", f"{TEAM}/instances")(lambda request: json_reply(_instance(), 201))
    fake_api.route("POST", f"{BOX}/connect")(
        lambda request: error_reply("conflict", "the container stopped: start_failed", 409)
    )
    fake_api.route("POST", f"{BOX}/stop")(lambda request: json_reply(_container(state="stopped")))

    with pytest.raises(SandboxConnectionError, match="readiness failed") as failure:
        tools.scratch.create()
    restored = fake_api.calls("POST", f"{TEAM}/instances")

    assert failure.value.state == "the container stopped: start_failed"
    assert len(fake_api.calls("POST", f"{BOX}/stop")) == 1
    assert restored[0].json() == {"release_id": RELEASE}

    with pytest.raises(SandboxConnectionError):
        tools.scratch.create_from_memory_snapshot(SNAPSHOT)
    assert fake_api.calls("POST", f"{TEAM}/instances")[1].json() == {"snapshot_id": SNAPSHOT}


def _sandbox(fake_api: FakeApi) -> SandboxInstance:
    client = WorkloadsClient(ApiClient(endpoint=fake_api.url, token=TOKEN), "team")
    return SandboxInstance(container_id=CONTAINER, stub_id=RELEASE, client=client)


def _process(running: bool, **extra: object) -> dict[str, object]:
    return {
        "process_id": "p1",
        "pid": 42,
        "command": "echo hi",
        "running": running,
        "stdout": "",
        "stderr": "",
        "stdout_truncated": False,
        "stderr_truncated": False,
        **extra,
    }


def test_sandbox_processes_start_poll_stream_and_kill(fake_api: FakeApi) -> None:
    sandbox = _sandbox(fake_api)
    fake_api.route("POST", f"{BOX}/processes")(lambda request: json_reply(_process(True), 201))
    polls: list[ApiRequest] = []

    @fake_api.route("GET", f"{BOX}/processes/p1")
    def get(request: ApiRequest) -> Reply:
        polls.append(request)
        if len(polls) == 1:
            return json_reply(_process(True, stdout="h"))
        return json_reply(_process(False, exit_code=0, stdout="hi\n", stderr="warn\n"))

    fake_api.route("POST", f"{BOX}/processes/p1/kill")(_no_content)
    fake_api.route("GET", f"{BOX}/processes")(
        lambda request: json_reply({"processes": [{**_process(True), "cwd": "/tmp"}]})
    )

    result = sandbox.run("echo 'hi there'", cwd="/tmp", env={"A": "1"})
    process = sandbox.process.exec("sleep", "10")
    process.kill()
    listed = sandbox.list_processes()

    assert fake_api.calls("POST", f"{BOX}/processes")[0].json() == {
        "args": ["echo", "hi there"],
        "cwd": "/tmp",
        "env": {"A": "1"},
    }
    assert fake_api.calls("POST", f"{BOX}/processes")[1].json() == {
        "args": ["sleep", "10"],
        "cwd": "/workspace",
    }
    assert polls[0].query == {"wait_seconds": ["5"]}
    assert (result.exit_code, result.stdout, result.stderr) == (0, "hi\n", "warn\n")
    assert len(fake_api.calls("POST", f"{BOX}/processes/p1/kill")) == 1
    assert [(item.pid, item.command) for item in listed] == [(42, "echo hi")]
    assert sandbox.process.list_processes()[42].cwd == "/tmp"


def test_sandbox_file_operations_map_to_the_container_file_routes(
    tmp_path: Path, fake_api: FakeApi
) -> None:
    sandbox = _sandbox(fake_api)
    files = f"{BOX}/files"
    entry = {
        "name": "a.txt",
        "mode": 0o100644,
        "size": 3,
        "mod_time": NOW,
        "owner": "0",
        "group": "0",
        "is_dir": False,
        "permissions": 0o644,
    }
    fake_api.route("PUT", f"{files}/content")(_no_content)
    fake_api.route("GET", f"{files}/content")(lambda request: (200, {}, b"abc"))
    fake_api.route("GET", f"{files}/stat")(lambda request: json_reply(entry))
    fake_api.route("GET", files)(
        lambda request: json_reply(
            {"files": [entry], "truncated": request.query["path"] == ["big"]}
        )
    )
    fake_api.route("DELETE", files)(_no_content)
    fake_api.route("POST", f"{BOX}/directories")(_no_content)
    fake_api.route("DELETE", f"{BOX}/directories")(_no_content)
    fake_api.route("POST", f"{files}/find")(
        lambda request: json_reply(
            {
                "matches": [
                    {"path": "a.txt", "line": 1, "column": 2, "text": "abc"},
                    {"path": "a.txt", "line": 3, "column": 1, "text": "b"},
                    {"path": "b.txt", "line": 1, "column": 1, "text": "b"},
                ],
                "truncated": False,
            }
        )
    )
    fake_api.route("POST", f"{files}/replace")(
        lambda request: json_reply({"files": 1, "replacements": 2})
    )
    local = tmp_path / "a.txt"
    local.write_bytes(b"abc")

    sandbox.fs.upload_file(local, "data/a.txt")
    sandbox.fs.download_file("data/a.txt", tmp_path / "out" / "a.txt")
    info = sandbox.fs.stat_file("data/a.txt")
    listed = sandbox.fs.list_files("data")
    sandbox.fs.create_directory("data/new", mode=0o700)
    sandbox.fs.delete_directory("data/new")
    sandbox.fs.delete_file("data/a.txt")
    found = sandbox.fs.find_in_files("data", "b")
    sandbox.fs.replace_in_files("data", "b", "c")

    (upload,) = fake_api.calls("PUT", f"{files}/content")
    assert (upload.query, upload.body) == ({"path": ["data/a.txt"], "mode": ["420"]}, b"abc")
    assert (tmp_path / "out" / "a.txt").read_bytes() == b"abc"
    assert (info.name, info.size, info.permissions) == ("a.txt", 3, 0o644)
    assert [item.name for item in listed] == ["a.txt"]
    assert fake_api.calls("POST", f"{BOX}/directories")[0].query == {
        "path": ["data/new"],
        "mode": ["448"],
    }
    assert fake_api.calls("DELETE", files)[0].query == {"path": ["data/a.txt"]}
    assert [(r.path, [m.range.start.line for m in r.matches]) for r in found] == [
        ("a.txt", [1, 3]),
        ("b.txt", [1]),
    ]
    assert found[0].matches[0].range.end.column == 3
    assert fake_api.calls("POST", f"{files}/replace")[0].json() == {
        "path": "data",
        "pattern": "b",
        "replacement": "c",
    }
    with pytest.raises(SandboxFileSystemError, match="more entries"):
        sandbox.fs.list_files("big")


def test_sandbox_ports_network_lifetime_snapshots_and_termination(fake_api: FakeApi) -> None:
    sandbox = _sandbox(fake_api)
    fake_api.route("POST", f"{BOX}/ports")(
        lambda request: json_reply({"port": 3000, "url": "https://3000.example.test"})
    )
    fake_api.route("GET", f"{BOX}/ports")(
        lambda request: json_reply({"ports": [{"port": 3000, "url": "https://3000.example.test"}]})
    )
    fake_api.route("PUT", f"{BOX}/network")(lambda request: json_reply(request.json()))
    fake_api.route("GET", f"{BOX}/network")(
        lambda request: json_reply({"block_network": True, "allow_list": []})
    )
    fake_api.route("PUT", f"{BOX}/ttl")(lambda request: json_reply({"ttl": 120}))
    fake_api.route("POST", f"{BOX}/snapshots")(
        lambda request: json_reply(
            {
                "id": SNAPSHOT,
                "container_id": CONTAINER,
                "release_id": RELEASE,
                "state": "available",
                "created_at": NOW,
            },
            201,
        )
    )
    fake_api.route("POST", f"{BOX}/filesystem-images")(
        lambda request: json_reply({"image_id": IMAGE_ID}, 201)
    )
    fake_api.route("POST", f"{BOX}/stop")(lambda request: json_reply(_container(state="stopped")))

    url = sandbox.expose_port(3000)
    urls = sandbox.list_urls()
    policy = sandbox.update_network_permissions(allow_list=["10.0.0.0/8"])
    current = sandbox.network_permissions()
    sandbox.update_ttl(120)
    snapshot = sandbox.snapshot_memory()
    image = sandbox.create_image_from_filesystem()
    terminated = sandbox.terminate()

    assert url == "https://3000.example.test"
    assert urls == {3000: "https://3000.example.test"}
    assert (policy.block_network, policy.allow_list) == (False, ["10.0.0.0/8"])
    assert current.block_network is True
    assert fake_api.calls("PUT", f"{BOX}/ttl")[0].json() == {"ttl": 120}
    assert (snapshot, image, terminated) == (SNAPSHOT, IMAGE_ID, True)
    with pytest.raises(SandboxConnectionError, match="terminated"):
        sandbox.update_ttl(60)
    with pytest.raises(ValueError, match="cannot be combined"):
        sandbox.update_network_permissions(block_network=True, allow_list=["10.0.0.0/8"])


def test_sandbox_listing_stats_and_timeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_api: FakeApi
) -> None:
    tools = _project(tmp_path, monkeypatch)
    row = {
        "id": CONTAINER,
        "release_id": RELEASE,
        "app": "tools",
        "name": "scratch",
        "status": "running",
        "gpu": [],
        "created_at": NOW,
    }
    fake_api.route("GET", f"{TEAM}/sandboxes")(lambda request: json_reply({"sandboxes": [row]}))
    fake_api.route("GET", f"{TEAM}/sandboxes/stats")(
        lambda request: json_reply(
            {
                "concurrent": 1,
                "total_created": 3,
                "rate_per_second": 0.0,
                "status_counts": {"running": 1},
                "created_buckets": [],
            }
        )
    )
    fake_api.route("GET", f"{BOX}/lifecycle")(
        lambda request: json_reply(
            {
                "container_id": CONTAINER,
                "state": "stopped",
                "stop_reason": "crashed",
                "created_at": "2026-10-01T12:00:00Z",
                "assigned_at": "2026-10-01T12:00:01Z",
                "ready_at": "2026-10-01T12:00:03Z",
                "stopped_at": "2026-10-01T12:01:03Z",
                "stages": [],
            }
        )
    )

    rows = tools.scratch.list(app_id="tools", limit=5)
    stats = tools.scratch.stats()
    timeline = tools.scratch.timeline(RELEASE)

    assert fake_api.calls("GET", f"{TEAM}/sandboxes")[0].query == {
        "app": ["tools"],
        "limit": ["5"],
    }
    assert [str(item.id) for item in rows] == [CONTAINER]
    assert stats.total_created == 3
    assert timeline.status.value == "failed"
    assert (timeline.scheduling_ms, timeline.startup_ms, timeline.runtime_ms) == (
        1000,
        3000,
        60000,
    )


def test_container_attach_follows_a_pod_command_to_its_exit_code(fake_api: FakeApi) -> None:
    states = iter(["ready", "stopped"])
    fake_api.route("GET", BOX)(
        lambda request: json_reply(
            _container(state=(state := next(states, "stopped")), exit_code=3 if state else None)
        )
    )
    fake_api.route("GET", f"{BOX}/output")(
        lambda request: (
            200,
            {"Content-Type": "application/x-ndjson"},
            [
                json.dumps({"id": 1, "stream": "stdout", "data": "serving", "time": NOW}).encode()
                + b"\n"
            ],
        )
    )

    result = _cli("container", "attach", CONTAINER)

    assert result.exit_code == 3, result.output
    assert "serving" in result.output
    assert "Exit code" in result.output
    assert fake_api.calls("GET", f"{BOX}/output")[0].query == {"after": ["0"], "follow": ["true"]}


def test_container_checkpoint_snapshots_the_container(fake_api: FakeApi) -> None:
    fake_api.route("POST", f"{BOX}/snapshots")(
        lambda request: json_reply(
            {
                "id": request.json()["snapshot_id"],
                "container_id": CONTAINER,
                "release_id": RELEASE,
                "state": "available",
                "created_at": NOW,
            },
            201,
        )
    )

    result = _cli("container", "checkpoint", CONTAINER, "--checkpoint-id", SNAPSHOT)

    assert result.exit_code == 0, result.output
    assert f"Created checkpoint {SNAPSHOT}." in result.output


def _host(pod: str = "box", role: str = "devbox") -> dict[str, object]:
    return {
        "app": "tools",
        "pod": pod,
        "role": role,
        "deployment_id": DEPLOYMENT,
        "alias": f"lazycloud-team-tools-{pod}",
        "host_public_key": HOST_KEY,
    }


def test_devbox_list_pages_and_status_reports_the_devbox(fake_api: FakeApi) -> None:
    @fake_api.route("GET", f"{TEAM}/ssh/hosts")
    def hosts(request: ApiRequest) -> Reply:
        if request.query.get("pod") == ["box"]:
            return json_reply({"hosts": [_host()]})
        return json_reply({"hosts": [_host()], "next_cursor": "c2"})

    fake_api.route("GET", f"{TEAM}/deployments/{DEPLOYMENT}/devbox")(
        lambda request: json_reply(
            {
                "deployment_id": DEPLOYMENT,
                "name": "box",
                "app": "tools",
                "ssh_command": "lazycloud devbox box ssh",
                "ssh_host": "lazycloud-team-tools-box",
                "state": "stopped",
                "phase": "failed",
                "phase_reason": "image pull failed",
                "open_connections": 0,
                "disk": {
                    "name": "box",
                    "size_bytes": 10 * 1024**3,
                    "stored_bytes": 0,
                    "generation": 1,
                    "status": "detached",
                },
            }
        )
    )
    fake_api.route("GET", f"{TEAM}/apps/tools/functions/box")(
        lambda request: json_reply(
            {
                "name": "box",
                "app": "tools",
                "state": "active",
                "active_release": _release(
                    {
                        "name": "box",
                        "source": {"sha256": "0" * 64},
                        "image": {"python_version": "3.12"},
                        "resources": {"cpu_millis": 2000, "memory_mib": 4096},
                    }
                ),
            }
        )
    )

    listed = _cli("devbox", "list", "--limit", "1")
    status = _cli("devbox", "box", "status")

    assert listed.exit_code == 0, listed.output
    assert "Next page: --cursor c2" in listed.output
    assert fake_api.calls("GET", f"{TEAM}/ssh/hosts")[0].query == {
        "role": ["devbox"],
        "limit": ["1"],
    }
    assert status.exit_code == 0, status.output
    for text in ("team/tools/box", "failed", "4Gi", "10.7 GB", "image pull failed"):
        assert text in status.output, status.output


def test_ssh_names_why_a_pod_cannot_be_reached(fake_api: FakeApi) -> None:
    fake_api.route("GET", f"{TEAM}/ssh/hosts")(lambda request: json_reply({"hosts": []}))

    @fake_api.route("GET", f"{TEAM}/deployments")
    def deployments(request: ApiRequest) -> Reply:
        if request.query.get("name") == ["web"]:
            return json_reply({"deployments": [_deployed(state="stopped")]})
        return json_reply({"deployments": []})

    missing = _cli("ssh", "nothing")
    stopped = _cli("ssh", "web")

    assert missing.exit_code != 0
    assert "no devbox or pod named 'nothing'" in str(missing.exception)
    assert stopped.exit_code != 0
    assert "is stopped" in str(stopped.exception)


def test_ssh_config_writes_hosts_a_certificate_and_the_user_include(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_api: FakeApi
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    fake_api.route("GET", f"{TEAM}/ssh/hosts")(lambda request: json_reply({"hosts": [_host()]}))
    fake_api.route("POST", f"{TEAM}/ssh/certificates")(
        lambda request: json_reply(
            {
                "certificate": "ssh-ed25519-cert-v01@openssh.com AAAA lazycloud",
                "principal": "root",
                "expires_at": NOW,
            },
            201,
        )
    )

    result = _cli("ssh-config")

    assert result.exit_code == 0, result.output
    assert "ssh lazycloud-team-tools-box" in result.output
    assert (
        fake_api.calls("POST", f"{TEAM}/ssh/certificates")[0]
        .json()["public_key"]
        .startswith("ssh-ed25519 ")
    )
    assert "Include" in (tmp_path / ".ssh" / "config").read_text()
