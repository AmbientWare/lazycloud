"""Which app or workload `lazycloud deploy` selects from a file or handler reference."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from lazycloud.cli.handler_workflows import HandlerLoadError
from lazycloud.cli.main import build_public_cli
from typer.testing import CliRunner, Result

from tests.api_server import ApiRequest, FakeApi, Reply, json_reply

cli = build_public_cli()

pytestmark = pytest.mark.usefixtures("isolated_imports")


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_api: FakeApi) -> FakeApi:
    """A working directory whose deploy previews answer every app's plan."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])

    @fake_api.route("POST", "/v1/workspaces/team/apps/[^/]+/deployment-plan")
    def plan(request: ApiRequest) -> Reply:
        items: list[dict[str, object]] = [
            {"kind": item["kind"], "name": item["name"], "action": "add", "versions": 0}
            for item in request.json()["workloads"]
        ]
        return json_reply({"app": request.path.split("/")[5], "prune": False, "items": items})

    return fake_api


def _preview(target: str) -> Result:
    return CliRunner().invoke(cli, ["deploy", target, "--diff"])


def _planned(api: FakeApi) -> list[tuple[str, list[str]]]:
    return [
        (r.path.split("/")[5], [item["name"] for item in r.json()["workloads"]])
        for r in api.calls("POST", "/v1/workspaces/team/apps/[^/]+/deployment-plan")
    ]


def test_file_deploy_selects_the_whole_app_and_deduplicates_aliases(
    tmp_path: Path, project: FakeApi
) -> None:
    (tmp_path / "app_definition.py").write_text(
        'from lazycloud import App\napp = App("example")\npod = app.pod(name="worker")\n',
        encoding="utf-8",
    )
    (tmp_path / "workloads.py").write_text(
        "from app_definition import app\nalias = app\n@app.function()\ndef hello(): return 1\n",
        encoding="utf-8",
    )

    for target in ("workloads.py", "workloads:app"):
        result = _preview(target)
        assert result.exit_code == 0, result.output

    assert _planned(project) == [("example", ["worker", "hello"])] * 2


def test_file_deploy_requires_a_selection_when_several_apps_are_present(
    tmp_path: Path, project: FakeApi
) -> None:
    (tmp_path / "several_apps.py").write_text(
        'from lazycloud import App\nfirst = App("first")\nsecond = App("second")\n'
        "@second.function()\ndef job(): return 1\n",
        encoding="utf-8",
    )

    ambiguous = _preview("several_apps.py")
    selected = _preview("several_apps.py:second")

    assert isinstance(ambiguous.exception, HandlerLoadError)
    message = str(ambiguous.exception)
    assert "multiple apps found" in message
    assert "several_apps.py:first" in message and "several_apps.py:second" in message
    assert selected.exit_code == 0, selected.output
    assert _planned(project) == [("second", ["job"])]


def test_file_deploy_rejects_a_module_without_an_app(tmp_path: Path, project: FakeApi) -> None:
    (tmp_path / "empty_app.py").write_text("def helper(): return 1\n", encoding="utf-8")

    result = _preview("empty_app.py")

    assert isinstance(result.exception, HandlerLoadError)
    assert "no App found" in str(result.exception)
    assert project.requests == []


def test_a_missing_handler_names_the_module_and_what_it_defines(
    tmp_path: Path, project: FakeApi
) -> None:
    (tmp_path / "dev.py").write_text(
        'from lazycloud import App\napp = App("dev")\n@app.function()\ndef hello(): return 1\n',
        encoding="utf-8",
    )

    result = _preview("dev:scratch")

    assert isinstance(result.exception, HandlerLoadError)
    details = result.exception.details
    assert details.type == "invalid_handler"
    assert "'scratch'" in details.message
    assert "dev:hello" in details.hint
    assert project.requests == []
