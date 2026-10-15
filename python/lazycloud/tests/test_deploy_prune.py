"""`lazycloud deploy --prune` across several apps."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from lazycloud.cli.main import build_public_cli
from typer.testing import CliRunner

from tests.api_server import ApiRequest, FakeApi, Reply, json_reply

cli = build_public_cli()

pytestmark = pytest.mark.usefixtures("isolated_imports")

NOW = "2026-10-01T00:00:00Z"
TEAM = "/v1/workspaces/team"
APPS = """\
import lazycloud

live = lazycloud.App("live")
gone = lazycloud.App("gone")


@live.function()
def job() -> int:
    return 1
"""


def _serve(api: FakeApi) -> None:
    @api.route("POST", f"{TEAM}/images/resolve")
    def resolve(request: ApiRequest) -> Reply:
        image: dict[str, object] = {
            "id": "img_0123456789abcdef01234567",
            "python_version": request.json()["python_version"],
            "architecture": "amd64",
            "ready": True,
            "created_at": NOW,
        }
        return json_reply({"image": image})

    @api.route("POST", f"{TEAM}/sources")
    def sources(request: ApiRequest) -> Reply:
        return json_reply({"sha256": request.json()["sha256"], "present": True})

    @api.route("POST", f"{TEAM}/apps/[^/]+/deployment-plan")
    def plan(request: ApiRequest) -> Reply:
        listed: list[dict[str, Any]] = request.json()["workloads"]
        items: list[dict[str, object]] = [
            {"kind": item["kind"], "name": item["name"], "action": "add", "versions": 0}
            for item in listed
        ]
        return json_reply({"app": request.path.split("/")[5], "prune": True, "items": items})

    @api.route("POST", f"{TEAM}/apps/[^/]+/deployments")
    def deploy(request: ApiRequest) -> Reply:
        workloads: list[dict[str, Any]] = request.json()["workloads"]
        app: dict[str, object] = {
            "id": "0192f0a0-0000-7000-8000-000000000001",
            "name": request.path.split("/")[5],
            "state": "active",
            "workloads": len(workloads),
            "running_containers": 0,
            "created_at": NOW,
        }
        releases: list[dict[str, object]] = [
            {
                "id": "0192f0a0-0000-7000-8000-000000000002",
                "name": spec["name"],
                "version": 1,
                "created_at": NOW,
                "spec": spec,
            }
            for spec in workloads
        ]
        return json_reply({"app": app, "releases": releases, "pruned": [], "removed_versions": 0})


def test_every_app_takes_its_workloads_before_any_app_is_pruned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_api: FakeApi
) -> None:
    (tmp_path / "apps.py").write_text(APPS, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])
    _serve(fake_api)

    result = CliRunner().invoke(cli, ["deploy", "apps.py:live", "apps.py:gone", "--prune"])

    assert result.exit_code == 0, result.output
    sent = [
        (r.path.split("/")[5], r.json()["prune"], [w["name"] for w in r.json()["workloads"]])
        for r in fake_api.calls("POST", f"{TEAM}/apps/[^/]+/deployments")
    ]
    # An app with no workloads only prunes, after every app has its workloads.
    assert sent == [
        ("live", False, ["job"]),
        ("live", True, ["job"]),
        ("gone", True, []),
    ]
