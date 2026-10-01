from __future__ import annotations

import base64
import hashlib
import importlib
import io
import json
import pickle
import sys
import zipfile
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from lazycloud.cli.components.errors import ClientError
from lazycloud.cli.main import build_public_cli, start
from lazycloud.config import get_profile, reset_settings_cache
from lazycloud.exceptions import (
    FunctionNotDeployedError,
    RemoteTaskError,
    UnsupportedFeatureError,
)
from lazycloud.values import cloudpickle_bytes
from typer.testing import CliRunner

from tests.api_server import (
    TOKEN,
    WORKSPACE,
    ApiRequest,
    FakeApi,
    Reply,
    StreamAborted,
    error_reply,
    json_reply,
)

pytestmark = pytest.mark.usefixtures("isolated_imports")

APP_ID = "0192f0a0-0000-7000-8000-000000000001"
RELEASE_ID = "0192f0a0-0000-7000-8000-000000000002"
NOW = "2026-09-30T12:00:00Z"
TASKS = "/v1/workspaces/team/apps/reports/functions/summarize_sales/tasks"

REPORTS = """\
import lazycloud

app = lazycloud.App("reports")


@app.function(
    image=lazycloud.Image(python_version="3.11"),
    cpu=0.5,
    memory="1Gi",
    timeout_seconds=120,
    retries=2,
    retry_delay_seconds=1.5,
    concurrency=4,
    keep_warm=30,
    max_pending_tasks=50,
    autoscaler={"max_containers": 3, "tasks_per_container": 2},
    env={"MODE": "test"},
)
def summarize_sales(values: list[int]) -> int:
    return sum(values)
"""


def _project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str = REPORTS) -> ModuleType:
    (tmp_path / "reports.py").write_text(source, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])
    sys.modules.pop("reports", None)
    return importlib.import_module("reports")


def _task(task_id: str, status: str = "queued", **extra: object) -> dict[str, object]:
    return {
        "id": task_id,
        "app": "reports",
        "function": "summarize_sales",
        "release_id": RELEASE_ID,
        "status": status,
        "attempts": 1 if status != "queued" else 0,
        "created_at": NOW,
        **extra,
    }


def _task_id(index: int) -> str:
    return f"0192f0a0-0000-7000-8000-{index:012d}"


def _pickled(value: object) -> str:
    return base64.b64encode(cloudpickle_bytes(value)).decode()


def _inputs(request: ApiRequest) -> list[dict[str, Any]]:
    body = request.json()
    decoded: list[dict[str, Any]] = []
    for item in body["inputs"]:
        assert item["encoding"] == "cloudpickle"
        decoded.append(pickle.loads(base64.b64decode(item["data"])))
    return decoded


def _serve_deployment(api: FakeApi, *, stored: set[str]) -> None:
    @api.route("POST", "/v1/workspaces/team/sources")
    def sources(request: ApiRequest) -> Reply:
        sha: str = request.json()["sha256"]
        state: dict[str, object] = {"sha256": sha, "present": sha in stored}
        if sha not in stored:
            state["upload"] = {
                "url": f"{api.url}/upload/{sha}",
                "method": "PUT",
                "headers": {"x-amz-checksum-sha256": sha},
                "expires_at": NOW,
            }
        return json_reply(state)

    @api.route("PUT", "/upload/[0-9a-f]{64}")
    def upload(request: ApiRequest) -> Reply:
        stored.add(request.path.rsplit("/", 1)[1])
        return 200, {}, b""

    @api.route("POST", "/v1/workspaces/team/apps/reports/deployments")
    def deploy(request: ApiRequest) -> Reply:
        functions: list[dict[str, Any]] = request.json()["functions"]
        releases: list[dict[str, object]] = [
            {
                "id": RELEASE_ID,
                "function": spec["name"],
                "version": 1,
                "created_at": NOW,
                "spec": spec,
            }
            for spec in functions
        ]
        app = {"id": APP_ID, "name": "reports", "state": "active", "created_at": NOW}
        return json_reply({"app": app, "releases": releases, "pruned": []})


def test_deploy_uploads_the_source_once_and_maps_function_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_api: FakeApi
) -> None:
    reports = _project(tmp_path, monkeypatch)
    stored: set[str] = set()
    _serve_deployment(fake_api, stored=stored)

    deployment = reports.app.deploy()

    assert [release.function for release in deployment.releases] == ["summarize_sales"]
    (upload,) = fake_api.calls("PUT", "/upload/.*")
    sha = upload.path.rsplit("/", 1)[1]
    assert hashlib.sha256(upload.body).hexdigest() == sha
    assert upload.headers["x-amz-checksum-sha256"] == sha
    assert "authorization" not in upload.headers
    assert "reports.py" in zipfile.ZipFile(io.BytesIO(upload.body)).namelist()
    assert len(fake_api.calls("POST", "/v1/workspaces/team/sources")) == 2
    (request,) = fake_api.calls("POST", "/v1/workspaces/team/apps/reports/deployments")
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.json() == {
        "functions": [
            {
                "name": "summarize_sales",
                "handler": "reports:summarize_sales",
                "source": {"sha256": sha},
                "image": {"python_version": "3.11"},
                "resources": {"cpu_millis": 500, "memory_mib": 1024},
                "retry_policy": {"max_attempts": 3, "delay_seconds": 1.5, "backoff": "fixed"},
                "concurrency": 4,
                "timeout_seconds": 120,
                "keep_warm_seconds": 30,
                "autoscaler": {"min_containers": 0, "max_containers": 3, "tasks_per_container": 2},
                "max_pending_tasks": 50,
                "environment": {"MODE": "test"},
            }
        ],
        "prune": False,
    }

    reports.summarize_sales.deploy()

    assert len(fake_api.calls("PUT", "/upload/.*")) == 1


RUNTIME_OPTIONS = """\
import lazycloud

app = lazycloud.App("reports")


def announce(ctx):
    print(ctx.hook)


@app.function(
    cron="Every 5m",
    secrets=["API_TOKEN", "DB_URL", "API_TOKEN"],
    callback_url=" https://hooks.example.com/tasks ",
    concurrency=4,
    in_process=True,
    keep_warm=-1,
    autoscaler={"min_containers": 1, "max_containers": 2},
    on_start=announce,
    on_retry=[announce, "reports:announce"],
)
def nightly() -> None:
    pass
"""


def test_deploy_maps_workload_runtime_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_api: FakeApi
) -> None:
    reports = _project(tmp_path, monkeypatch, RUNTIME_OPTIONS)
    _serve_deployment(fake_api, stored=set())

    reports.app.deploy()

    (request,) = fake_api.calls("POST", "/v1/workspaces/team/apps/reports/deployments")
    spec = request.json()["functions"][0]
    assert spec["cron"] == "Every 5m"
    assert spec["secrets"] == ["API_TOKEN", "DB_URL"]
    assert spec["callback_url"] == "https://hooks.example.com/tasks"
    assert spec["in_process"] is True
    assert spec["keep_warm_seconds"] == -1
    assert spec["lifecycle_hooks"] == {
        "on_start": ["reports:announce"],
        "on_retry": ["reports:announce", "reports:announce"],
    }


@pytest.mark.parametrize(
    ("decorator", "option"),
    [
        ('@app.function(gpu="A10G")', "gpu"),
        ("@app.function(docker_enabled=True)", "docker_enabled"),
        ('@app.function(image=lazycloud.Image(python_packages=["numpy"]))', "image packages"),
    ],
)
def test_unsupported_options_fail_before_any_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_api: FakeApi,
    decorator: str,
    option: str,
) -> None:
    source = f'import lazycloud\napp = lazycloud.App("reports")\n{decorator}\ndef job(): pass\n'
    reports = _project(tmp_path, monkeypatch, source)

    with pytest.raises(UnsupportedFeatureError, match=option):
        reports.app.deploy()
    with pytest.raises(UnsupportedFeatureError, match=option):
        reports.job.remote()

    assert fake_api.requests == []


def test_an_app_with_an_endpoint_names_it_instead_of_deploying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_api: FakeApi
) -> None:
    source = REPORTS + "\n@app.endpoint(name='api')\ndef api(): return {}\n"
    reports = _project(tmp_path, monkeypatch, source)

    with pytest.raises(UnsupportedFeatureError, match="endpoint:api"):
        reports.app.deploy()

    assert fake_api.requests == []


def test_remote_streams_output_resumes_dropped_logs_and_returns_the_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_api: FakeApi,
) -> None:
    reports = _project(tmp_path, monkeypatch)
    task_id = _task_id(7)
    log_streams: list[Iterator[bytes]] = []

    def entry(number: int, stream: str, data: str) -> bytes:
        record: dict[str, object] = {
            "id": number,
            "attempt": 1,
            "stream": stream,
            "data": data,
            "time": NOW,
        }
        return json.dumps(record).encode() + b"\n"

    def dropped() -> Iterator[bytes]:
        yield entry(1, "stdout", "summing 3 values")
        raise StreamAborted

    def finished() -> Iterator[bytes]:
        yield entry(2, "stderr", "total ready")

    log_streams.extend([dropped(), finished()])

    fake_api.route("POST", TASKS)(lambda _: json_reply({"tasks": [_task(task_id)]}, 201))
    fake_api.route("GET", f"/v1/workspaces/team/tasks/{task_id}/logs")(
        lambda _: (200, {"Content-Type": "application/x-ndjson"}, log_streams.pop(0))
    )
    fake_api.route("GET", f"/v1/workspaces/team/tasks/{task_id}")(
        lambda _: json_reply(_task(task_id, "succeeded"))
    )
    fake_api.route("GET", f"/v1/workspaces/team/tasks/{task_id}/result")(
        lambda _: json_reply({"encoding": "cloudpickle", "data": _pickled(5500)})
    )

    assert reports.summarize_sales.remote([1200, 3500, 800]) == 5500

    (submit,) = fake_api.calls("POST", TASKS)
    assert _inputs(submit) == [{"args": [[1200, 3500, 800]], "kwargs": {}}]
    logs = fake_api.calls("GET", f"/v1/workspaces/team/tasks/{task_id}/logs")
    assert [(r.query["after"], r.query["follow"]) for r in logs] == [
        (["0"], ["true"]),
        (["1"], ["true"]),
    ]
    (wait,) = fake_api.calls("GET", f"/v1/workspaces/team/tasks/{task_id}")
    assert wait.query["wait_seconds"] == ["30"]
    stderr = capsys.readouterr().err
    assert stderr.count("summing 3 values") == 1
    assert "total ready" in stderr


def _serve_failed_task(api: FakeApi, failure: dict[str, object]) -> None:
    task_id = _task_id(9)
    api.route("POST", TASKS)(lambda _: json_reply({"tasks": [_task(task_id)]}, 201))
    api.route("GET", f"/v1/workspaces/team/tasks/{task_id}/logs")(lambda _: (200, {}, b""))
    api.route("GET", f"/v1/workspaces/team/tasks/{task_id}")(
        lambda _: json_reply(_task(task_id, "failed", failure=failure))
    )


def test_remote_failure_reraises_the_remote_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_api: FakeApi
) -> None:
    reports = _project(tmp_path, monkeypatch)
    _serve_failed_task(
        fake_api,
        {
            "kind": "user_error",
            "type": "ValueError",
            "message": "no sales",
            "traceback": "Traceback (most recent call last):\nValueError: no sales\n",
            "exception": _pickled(ValueError("no sales")),
        },
    )

    with pytest.raises(ValueError, match="no sales") as raised:
        reports.summarize_sales.remote([])

    cause = raised.value.__cause__
    assert isinstance(cause, RemoteTaskError)
    assert cause.kind == "user_error"
    assert "Remote traceback" in str(cause)


def test_remote_failure_without_an_exception_raises_a_typed_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_api: FakeApi
) -> None:
    reports = _project(tmp_path, monkeypatch)
    _serve_failed_task(
        fake_api,
        {"kind": "timeout", "message": "attempt exceeded 120 seconds"},
    )

    with pytest.raises(RemoteTaskError) as raised:
        reports.summarize_sales.remote([1])

    assert raised.value.kind == "timeout"
    assert raised.value.type is None
    assert raised.value.message == "attempt exceeded 120 seconds"


def test_spawn_map_submits_in_batches_and_keeps_input_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_api: FakeApi
) -> None:
    reports = _project(tmp_path, monkeypatch)
    submitted: list[int] = []

    @fake_api.route("POST", TASKS)
    def submit(request: ApiRequest) -> Reply:
        tasks: list[dict[str, object]] = []
        for payload in _inputs(request):
            submitted.append(payload["args"][0])
            tasks.append(_task(_task_id(submitted[-1])))
        return json_reply({"tasks": tasks}, 201)

    fake_api.route("GET", "/v1/workspaces/team/tasks/[^/]+")(
        lambda request: json_reply(_task(request.path.rsplit("/", 1)[1], "succeeded"))
    )
    fake_api.route("GET", "/v1/workspaces/team/tasks/[^/]+/result")(
        lambda request: json_reply(
            {"encoding": "cloudpickle", "data": _pickled(int(request.path.split("/")[-2][-12:]))}
        )
    )

    calls = reports.summarize_sales.spawn_map([[value] for value in range(1001)])

    assert [len(_inputs(r)) for r in fake_api.calls("POST", TASKS)] == [1000, 1]
    assert submitted == list(range(1001))
    assert [call.get() for call in calls[995:]] == list(range(995, 1001))


def test_calling_an_undeployed_function_raises_a_typed_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_api: FakeApi
) -> None:
    reports = _project(tmp_path, monkeypatch)
    fake_api.route("POST", TASKS)(lambda _: error_reply("not_found", "function not found", 404))

    with pytest.raises(FunctionNotDeployedError, match=r"reports\.summarize_sales"):
        reports.summarize_sales.spawn([1])


def _me(*workspaces: str) -> Reply:
    return json_reply(
        {
            "user": {"id": APP_ID, "email": "dev@example.com"},
            "workspaces": [
                {"id": _task_id(index), "name": name} for index, name in enumerate(workspaces)
            ],
        }
    )


def test_login_verifies_the_token_and_stores_the_only_workspace(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fake_api: FakeApi
) -> None:
    monkeypatch.delenv("LAZYCLOUD_TOKEN")
    monkeypatch.delenv("LAZYCLOUD_WORKSPACE")
    reset_settings_cache()
    fake_api.route("GET", "/v1/me")(lambda _: _me("analytics"))

    result = CliRunner().invoke(build_public_cli(), ["login", "--token", "lc_new-token-value"])

    assert result.exit_code == 0, result.output
    (me,) = fake_api.calls("GET", "/v1/me")
    assert me.headers["authorization"] == "Bearer lc_new-token-value"
    profile = get_profile()
    assert (profile.token, profile.workspace) == ("lc_new-token-value", "analytics")

    fake_api.route("GET", "/v1/me")(lambda _: _me("analytics", "billing"))
    result = CliRunner().invoke(
        build_public_cli(), ["login", "--profile", "other", "--token", "lc_other-token-1"]
    )
    assert isinstance(result.exception, ClientError)
    assert "analytics, billing" in result.exception.details.hint

    fake_api.route("GET", "/v1/me")(lambda _: error_reply("unauthenticated", "bad token", 401))
    with pytest.raises(SystemExit):
        start(args=["--json", "login", "--token", "lc_bad-token-1"], prog_name="lazycloud")
    assert json.loads(capsys.readouterr().err)["error"]["type"] == "authentication_failed"


def test_cli_run_json_and_task_commands_use_the_task_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_api: FakeApi
) -> None:
    _project(tmp_path, monkeypatch)
    task_id = _task_id(11)
    fake_api.route("POST", TASKS)(lambda _: json_reply({"tasks": [_task(task_id)]}, 201))
    fake_api.route("GET", f"/v1/workspaces/team/tasks/{task_id}/logs")(lambda _: (200, {}, b""))
    fake_api.route("GET", f"/v1/workspaces/team/tasks/{task_id}")(
        lambda _: json_reply(_task(task_id, "succeeded"))
    )
    fake_api.route("GET", f"/v1/workspaces/team/tasks/{task_id}/result")(
        lambda _: json_reply({"encoding": "json", "value": 6})
    )
    fake_api.route("POST", f"/v1/workspaces/team/tasks/{task_id}/cancel")(
        lambda _: json_reply(_task(task_id, "cancelled"))
    )
    cli = build_public_cli()

    ran = CliRunner().invoke(cli, ["--json", "run", "reports:summarize_sales", "[1, 2, 3]"])
    result = CliRunner().invoke(cli, ["--json", "task", "result", task_id])
    cancelled = CliRunner().invoke(cli, ["--json", "task", "cancel", task_id])

    assert ran.exit_code == 0, ran.output
    assert json.loads(ran.stdout) == 6
    (submit,) = fake_api.calls("POST", TASKS)
    arguments: dict[str, object] = {"args": [[1, 2, 3]], "kwargs": {}}
    assert submit.json() == {"inputs": [{"encoding": "json", "value": arguments}]}
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == 6
    assert cancelled.exit_code == 0, cancelled.output
    assert json.loads(cancelled.stdout)["status"] == "cancelled"
    assert fake_api.calls("POST", f"/v1/workspaces/{WORKSPACE}/tasks/{task_id}/cancel")


def test_cli_deploy_of_a_file_deploys_its_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_api: FakeApi
) -> None:
    (tmp_path / "reports.py").write_text(REPORTS, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _serve_deployment(fake_api, stored=set())

    result = CliRunner().invoke(build_public_cli(), ["--json", "deploy", "reports.py"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["releases"][0]["function"] == "summarize_sales"
    (request,) = fake_api.calls("POST", "/v1/workspaces/team/apps/reports/deployments")
    assert request.json()["functions"][0]["handler"] == "reports:summarize_sales"
