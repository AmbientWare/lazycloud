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
from lazycloud.session.deployment import DeploymentOperationError
from lazycloud.terminal import output
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
IMAGE_ID = "img_0123456789abcdef01234567"
BUILD_ID = "0192f0a0-0000-7000-8000-0000000000b1"
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


def _image(ready: bool, python_version: str = "3.11") -> dict[str, object]:
    return {
        "id": IMAGE_ID,
        "python_version": python_version,
        "architecture": "amd64",
        "ready": ready,
        "created_at": NOW,
    }


def _serve_deployment(api: FakeApi, *, stored: set[str]) -> None:
    api.route("POST", "/v1/workspaces/team/images/resolve")(
        lambda request: json_reply(
            {"image": _image(True, python_version=request.json()["python_version"])}
        )
    )

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
                "image": {"python_version": "3.11", "image_id": IMAGE_ID},
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


@pytest.mark.parametrize(
    ("decorator", "option"),
    [
        ('@app.function(gpu="A10G")', "gpu"),
        ('@app.function(secrets=["api-key"])', "secrets"),
        ('@app.function(cron="0 * * * *")', "cron"),
        ("@app.function(on_start=print)", "on_start"),
        (
            "@app.function(volumes=[lazycloud.CloudBucket("
            '"models", "/models", lazycloud.CloudBucketConfig())])',
            "cloud bucket",
        ),
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


IMAGE_APP = """\
import lazycloud

app = lazycloud.App("reports")
image = lazycloud.Image(python_packages=["numpy"])


@app.function(image=image)
def first(): pass


@app.function(image=lazycloud.Image(python_packages=["numpy"]))
def second(): pass
"""


def _serve_image_build(api: FakeApi, final: dict[str, object]) -> None:
    build: dict[str, object] = {
        "id": BUILD_ID,
        "image_id": IMAGE_ID,
        "status": "building",
        "phase": "queued",
        "attempt": 1,
        "created_at": NOW,
    }
    api.route("POST", "/v1/workspaces/team/images/resolve")(
        lambda _: json_reply({"image": _image(False, "3.12")})
    )
    api.route("POST", "/v1/workspaces/team/images")(
        lambda _: json_reply({"image": _image(False, "3.12"), "build": build})
    )
    log: dict[str, object] = {
        "id": 1,
        "attempt": 1,
        "data": "#5 [2/3] RUN pip install numpy",
        "time": NOW,
    }
    api.route("GET", f"/v1/workspaces/team/image-builds/{BUILD_ID}/logs")(
        lambda _: (200, {"Content-Type": "application/x-ndjson"}, json.dumps(log).encode())
    )
    api.route("GET", f"/v1/workspaces/team/image-builds/{BUILD_ID}")(
        lambda _: json_reply({**build, **final})
    )


def test_deploy_builds_each_distinct_image_once_and_sends_its_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_api: FakeApi,
) -> None:
    reports = _project(tmp_path, monkeypatch, IMAGE_APP)
    _serve_deployment(fake_api, stored=set())
    _serve_image_build(fake_api, {"status": "succeeded", "phase": "finished"})

    with output(enabled=True):
        reports.app.deploy()

    (resolve,) = fake_api.calls("POST", "/v1/workspaces/team/images/resolve")
    assert resolve.json()["python_packages"] == ["numpy"]
    assert len(fake_api.calls("POST", "/v1/workspaces/team/images")) == 1
    (deploy,) = fake_api.calls("POST", "/v1/workspaces/team/apps/reports/deployments")
    assert [spec["image"] for spec in deploy.json()["functions"]] == [
        {"python_version": "3.12", "image_id": IMAGE_ID}
    ] * 2
    assert "python 3.12 · built" in capsys.readouterr().err


def test_deploy_fails_with_the_build_id_when_an_image_build_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_api: FakeApi
) -> None:
    reports = _project(tmp_path, monkeypatch, IMAGE_APP)
    _serve_deployment(fake_api, stored=set())
    _serve_image_build(
        fake_api, {"status": "failed", "phase": "finished", "failure": "pip exited with 1"}
    )

    with pytest.raises(DeploymentOperationError, match=f"build {BUILD_ID} failed: pip exited"):
        reports.app.deploy()

    assert fake_api.calls("POST", "/v1/workspaces/team/apps/reports/deployments") == []


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


def _me(*workspaces: str, owned: str = "") -> Reply:
    return json_reply(
        {
            "user": {
                "id": APP_ID,
                "email": "dev@example.com",
                "display_name": "Dev",
                "avatar_url": "",
                "github_login": "dev",
                "is_admin": False,
                "created_at": NOW,
            },
            "workspaces": [
                {
                    "id": _task_id(index),
                    "name": name,
                    "state": "active",
                    "role": "owner" if name == owned else "member",
                    "created_at": NOW,
                }
                for index, name in enumerate(workspaces)
            ],
        }
    )


def test_every_command_tells_an_out_of_date_client_to_update(fake_api: FakeApi) -> None:
    def newer(_: ApiRequest) -> Reply:
        status, headers, body = _me("team")
        return status, {**headers, "X-Lazycloud-Recommended-Client-Version": "999.0.0"}, body

    fake_api.route("GET", "/v1/me")(newer)

    result = CliRunner().invoke(build_public_cli(), ["login"])

    assert result.exit_code == 0, result.output
    assert "this server recommends 999.0.0. Run `lazycloud update` to upgrade." in result.stderr
    (me,) = fake_api.calls("GET", "/v1/me")
    assert me.headers["user-agent"].startswith("lazycloud/")


def test_login_verifies_the_token_and_stores_the_owned_or_only_workspace(
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

    # Joining someone else's workspace does not change which one is yours.
    fake_api.route("GET", "/v1/me")(lambda _: _me("analytics", "billing", owned="billing"))
    result = CliRunner().invoke(
        build_public_cli(), ["login", "--profile", "mine", "--token", "lc_mine-token-1"]
    )
    assert result.exit_code == 0, result.output
    assert get_profile("mine").workspace == "billing"

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


def _device_codes(fake_api: FakeApi, outcomes: list[dict[str, object]]) -> list[float]:
    """Serve one device login whose polls answer `outcomes` in order."""

    fake_api.route("POST", "/v1/device-codes")(
        lambda _: json_reply(
            {
                "device_code": "dc_secret",
                "user_code": "BCDF-GHJK",
                "verification_uri": "https://dash.test/activate",
                "verification_uri_complete": "https://dash.test/activate?code=BCDF-GHJK",
                "expires_in_seconds": 900,
                "poll_interval_seconds": 5,
            },
            201,
        )
    )
    answers = iter(outcomes)
    fake_api.route("POST", "/v1/device-codes/token")(lambda _: json_reply(next(answers)))
    slept: list[float] = []
    return slept


def test_login_without_a_token_runs_the_device_flow(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fake_api: FakeApi
) -> None:
    from lazycloud.cli import identity as identity_commands

    monkeypatch.delenv("LAZYCLOUD_TOKEN")
    monkeypatch.delenv("LAZYCLOUD_WORKSPACE")
    reset_settings_cache()
    slept = _device_codes(
        fake_api,
        [
            {"status": "pending", "poll_interval_seconds": 5},
            {"status": "slow_down", "poll_interval_seconds": 10},
            {"status": "approved", "token": "lc_device-token-1", "poll_interval_seconds": 10},
        ],
    )
    monkeypatch.setattr(identity_commands.time, "sleep", slept.append)
    monkeypatch.setattr(identity_commands.socket, "gethostname", lambda: "laptop")
    fake_api.route("GET", "/v1/me")(lambda _: _me("analytics", owned="analytics"))

    result = CliRunner().invoke(build_public_cli(), ["--json", "login"])

    assert result.exit_code == 0, result.output
    # The card names the profile, the endpoint, the link and the code.
    card = result.stderr
    for text in (
        "Sign in to lazycloud",
        "Profile default at",
        fake_api.url,
        "https://dash.test/activate?code=BCDF-GHJK",
        "BCDF-GHJK",
        "15 minutes",
    ):
        assert text in card, card
    assert slept == [5.0, 5.0, 10.0]
    (started,) = fake_api.calls("POST", "/v1/device-codes")
    assert started.json() == {"client_name": "cli@laptop"}
    assert "authorization" not in started.headers
    polls = fake_api.calls("POST", "/v1/device-codes/token")
    assert [p.json() for p in polls] == [{"device_code": "dc_secret"}] * 3
    assert all("authorization" not in p.headers for p in polls)
    (me,) = fake_api.calls("GET", "/v1/me")
    assert me.headers["authorization"] == "Bearer lc_device-token-1"
    payload = json.loads(result.stdout)
    assert payload["token_source"] == "device"
    profile = get_profile()
    assert (profile.token, profile.workspace) == ("lc_device-token-1", "analytics")


@pytest.mark.parametrize(
    ("status", "message"),
    [("denied", "The sign-in request was denied."), ("expired", "The sign-in code expired.")],
)
def test_device_login_reports_denied_and_expired(
    status: str, message: str, monkeypatch: pytest.MonkeyPatch, fake_api: FakeApi
) -> None:
    from lazycloud.cli import identity as identity_commands

    monkeypatch.delenv("LAZYCLOUD_TOKEN")
    reset_settings_cache()
    slept = _device_codes(fake_api, [{"status": status, "poll_interval_seconds": 5}])
    monkeypatch.setattr(identity_commands.time, "sleep", slept.append)

    result = CliRunner().invoke(build_public_cli(), ["login", "--token", ""])

    assert isinstance(result.exception, ClientError)
    assert str(result.exception) == message
    assert get_profile().token == ""


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
