from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import typer
from lazycloud.cli import workspaces as workspace_commands
from lazycloud.cli.components.errors import ClientError
from lazycloud.cli.components.output import CliContextState
from lazycloud.cli.main import build_public_cli
from lazycloud.config import ClientProfile, get_profile, reset_settings_cache, set_profile
from typer.core import TyperCommand
from typer.testing import CliRunner

from tests.api_server import (
    TOKEN,
    ApiRequest,
    FakeApi,
    Reply,
    error_reply,
    json_reply,
    running_fake_api,
)


class _InteractiveInput:
    def isatty(self) -> bool:
        return True


class _Workspaces:
    """The workspace routes of the API over an in-memory list."""

    def __init__(self, api: FakeApi, *names: str) -> None:
        self.items: dict[str, dict[str, object]] = {}
        for name in names:
            self._add(name)
        api.route("GET", "/v1/workspaces")(self.list)
        api.route("POST", "/v1/workspaces")(self.create)
        api.route("PATCH", "/v1/workspaces/([a-z0-9-]+)")(self.rename)
        api.route("DELETE", "/v1/workspaces/([a-z0-9-]+)")(self.delete)

    def _add(self, name: str) -> dict[str, object]:
        item: dict[str, object] = {
            "id": f"0192f0a0-0000-7000-8000-{len(self.items):012d}",
            "name": name,
            "state": "active",
            "role": "owner",
            "created_at": "2026-09-01T00:00:00Z",
        }
        self.items[name] = item
        return item

    def list(self, request: ApiRequest) -> Reply:
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        return json_reply({"workspaces": sorted(self.items.values(), key=lambda w: str(w["name"]))})

    def create(self, request: ApiRequest) -> Reply:
        return json_reply(self._add(request.json()["name"]), 201)

    def rename(self, request: ApiRequest) -> Reply:
        old = request.path.rsplit("/", 1)[1]
        if old not in self.items:
            return error_reply("forbidden", "the token cannot reach this workspace", 403)
        item = self.items.pop(old)
        item["name"] = request.json()["name"]
        self.items[str(item["name"])] = item
        return json_reply(item)

    def delete(self, request: ApiRequest) -> Reply:
        item = self.items[request.path.rsplit("/", 1)[1]]
        item["state"] = "deleting"
        return json_reply(item, 202)


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[FakeApi]:
    monkeypatch.setenv("LAZYCLOUD_HOME", str(tmp_path / "state"))
    for name in (
        "LAZYCLOUD_ENDPOINT",
        "LAZYCLOUD_PROFILE",
        "LAZYCLOUD_TOKEN",
        "LAZYCLOUD_WORKSPACE",
    ):
        monkeypatch.delenv(name, raising=False)
    reset_settings_cache()
    with running_fake_api() as fake:
        yield fake
    reset_settings_cache()


def _profile(api: FakeApi, workspace: str) -> None:
    set_profile(ClientProfile(endpoint=api.url, workspace=workspace, token=TOKEN))


def test_workspace_create_and_rename_keep_the_profile_selected(api: FakeApi) -> None:
    _Workspaces(api, "default")
    _profile(api, "default")
    cli = build_public_cli()

    created = CliRunner().invoke(cli, ["--json", "workspace", "create", "review"])
    assert created.exit_code == 0, created.output
    assert json.loads(created.stdout)["name"] == "review"
    assert get_profile(apply_env=False).workspace == "review"
    (create,) = api.calls("POST", "/v1/workspaces")
    assert create.json() == {"name": "review"}

    renamed = CliRunner().invoke(cli, ["--json", "workspace", "rename", "renamed"])
    assert renamed.exit_code == 0, renamed.output
    assert json.loads(renamed.stdout)["name"] == "renamed"
    assert get_profile(apply_env=False).workspace == "renamed"
    (rename,) = api.calls("PATCH", "/v1/workspaces/review")
    assert rename.json() == {"name": "renamed"}

    listed = CliRunner().invoke(cli, ["--json", "workspace", "list"])
    assert [w["name"] for w in json.loads(listed.stdout)["workspaces"]] == ["default", "renamed"]


def test_workspace_create_in_a_cloud_names_the_cloud(api: FakeApi) -> None:
    _Workspaces(api, "default")
    _profile(api, "default")
    cli = build_public_cli()

    created = CliRunner().invoke(cli, ["--json", "workspace", "create", "review", "--cloud", "aws"])
    assert created.exit_code == 0, created.output
    (request,) = api.calls("POST", "/v1/workspaces")
    assert request.json() == {"name": "review", "cloud": "aws"}

    refused = CliRunner().invoke(cli, ["workspace", "create", "other", "--cloud", "gcp"])
    assert isinstance(refused.exception, ClientError)
    assert "unsupported cloud 'gcp'; connected clouds: aws" in str(refused.exception)
    assert len(api.calls("POST", "/v1/workspaces")) == 1


def test_workspace_use_selects_only_an_accessible_workspace(api: FakeApi) -> None:
    _Workspaces(api, "default", "team")
    _profile(api, "default")
    cli = build_public_cli()

    selected = CliRunner().invoke(cli, ["--json", "workspace", "use", "team"])
    assert selected.exit_code == 0, selected.output
    assert json.loads(selected.stdout)["current"] is True
    assert get_profile(apply_env=False).workspace == "team"

    missing = CliRunner().invoke(cli, ["--json", "workspace", "use", "missing"])
    assert missing.exit_code == 1
    assert get_profile(apply_env=False).workspace == "team"


def test_workspace_delete_requires_the_exact_name_and_selects_a_fallback(
    api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    _Workspaces(api, "default", "review")
    _profile(api, "review")

    def wrong_prompt(*_args: object, **_kwargs: object) -> str:
        return "wrong"

    monkeypatch.setattr(sys, "stdin", _InteractiveInput())
    monkeypatch.setattr(typer, "prompt", wrong_prompt)
    context = typer.Context(TyperCommand(name="delete"))
    context.obj = CliContextState()
    with pytest.raises(ClientError, match="confirmation did not match"):
        workspace_commands.workspace_delete(context, "review", False)
    assert not api.calls("DELETE", "/v1/workspaces/review")

    deleted = CliRunner().invoke(
        build_public_cli(),
        ["--json", "workspace", "delete", "review", "--yes"],
    )
    assert deleted.exit_code == 0, deleted.output
    assert json.loads(deleted.stdout) == {
        "current": "default",
        "deleted": True,
        "name": "review",
        "state": "deleting",
    }
    assert len(api.calls("DELETE", "/v1/workspaces/review")) == 1
    assert get_profile(apply_env=False).workspace == "default"


def test_workspace_create_refuses_an_environment_owned_selection(
    api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    _Workspaces(api, "default")
    _profile(api, "default")
    monkeypatch.setenv("LAZYCLOUD_WORKSPACE", "environment")
    reset_settings_cache()

    result = CliRunner().invoke(
        build_public_cli(),
        ["--json", "workspace", "create", "review"],
    )

    assert result.exit_code == 1
    assert not api.calls("POST", "/v1/workspaces")
