from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
import typer
from lazycloud.cli import workspaces as workspace_commands
from lazycloud.cli.components.errors import ClientError
from lazycloud.cli.components.output import CliContextState
from lazycloud.cli.main import build_public_cli
from lazycloud.config import ClientProfile, get_profile, reset_settings_cache, set_profile
from shared.http.workspaces import WorkspaceListResponse, WorkspaceResponse
from typer.core import TyperCommand
from typer.testing import CliRunner


class _InteractiveInput:
    def isatty(self) -> bool:
        return True


def _workspace(name: str) -> WorkspaceResponse:
    return WorkspaceResponse(
        id=f"workspace-{name}",
        name=name,
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
        updated_at=datetime(2026, 9, 1, tzinfo=UTC),
    )


@dataclass
class _WorkspaceClient:
    selected: WorkspaceResponse
    workspaces: list[WorkspaceResponse]
    deleted: list[str] = field(default_factory=list)

    def current(self) -> WorkspaceResponse:
        return self.selected

    def list(self) -> WorkspaceListResponse:
        return WorkspaceListResponse(workspaces=self.workspaces)

    def create(self, name: str) -> WorkspaceResponse:
        created = _workspace(name)
        self.workspaces.append(created)
        self.selected = created
        return created

    def rename(self, name: str) -> WorkspaceResponse:
        renamed = self.selected.model_copy(update={"name": name})
        self.workspaces = [
            renamed if item.id == self.selected.id else item for item in self.workspaces
        ]
        self.selected = renamed
        return renamed

    def delete(self, name: str) -> None:
        self.deleted.append(name)
        self.workspaces = [item for item in self.workspaces if item.name != name]


@pytest.fixture(autouse=True)
def isolated_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[None]:
    monkeypatch.setenv("LAZYCLOUD_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("LAZYCLOUD_ENDPOINT", raising=False)
    monkeypatch.delenv("LAZYCLOUD_PROFILE", raising=False)
    monkeypatch.delenv("LAZYCLOUD_TOKEN", raising=False)
    monkeypatch.delenv("LAZYCLOUD_WORKSPACE", raising=False)
    reset_settings_cache()
    yield
    reset_settings_cache()


def _profile(workspace: str) -> None:
    set_profile(
        ClientProfile(
            endpoint="https://api.example",
            workspace=workspace,
            token="stored-token",
        )
    )


def test_workspace_create_and_rename_keep_the_profile_selected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default = _workspace("default")
    client = _WorkspaceClient(selected=default, workspaces=[default])
    _profile(default.name)
    monkeypatch.setattr(workspace_commands, "workspace_client", lambda: client)
    cli = build_public_cli()

    created = CliRunner().invoke(cli, ["--json", "workspace", "create", "review"])
    assert created.exit_code == 0, created.output
    assert json.loads(created.stdout)["name"] == "review"
    assert get_profile(apply_env=False).workspace == "review"

    renamed = CliRunner().invoke(cli, ["--json", "workspace", "rename", "renamed"])
    assert renamed.exit_code == 0, renamed.output
    assert json.loads(renamed.stdout)["name"] == "renamed"
    assert get_profile(apply_env=False).workspace == "renamed"


def test_workspace_use_selects_only_an_accessible_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default = _workspace("default")
    team = _workspace("team")
    client = _WorkspaceClient(selected=default, workspaces=[default, team])
    _profile(default.name)
    monkeypatch.setattr(workspace_commands, "workspace_client", lambda: client)
    cli = build_public_cli()

    selected = CliRunner().invoke(cli, ["--json", "workspace", "use", "team"])
    assert selected.exit_code == 0, selected.output
    assert json.loads(selected.stdout)["current"] is True
    assert get_profile(apply_env=False).workspace == "team"

    missing = CliRunner().invoke(cli, ["--json", "workspace", "use", "missing"])
    assert missing.exit_code == 1
    assert get_profile(apply_env=False).workspace == "team"


def test_workspace_delete_requires_the_exact_name_and_selects_a_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default = _workspace("default")
    review = _workspace("review")
    client = _WorkspaceClient(selected=review, workspaces=[default, review])
    _profile(review.name)
    monkeypatch.setattr(workspace_commands, "workspace_client", lambda: client)

    def wrong_prompt(*_args: object, **_kwargs: object) -> str:
        return "wrong"

    monkeypatch.setattr(sys, "stdin", _InteractiveInput())
    monkeypatch.setattr(typer, "prompt", wrong_prompt)
    context = typer.Context(TyperCommand(name="delete"))
    context.obj = CliContextState()
    with pytest.raises(ClientError, match="confirmation did not match"):
        workspace_commands.workspace_delete(context, review.name, False)
    assert client.deleted == []

    deleted = CliRunner().invoke(
        build_public_cli(),
        ["--json", "workspace", "delete", review.name, "--yes"],
    )
    assert deleted.exit_code == 0, deleted.output
    assert json.loads(deleted.stdout) == {
        "current": "default",
        "deleted": True,
        "name": "review",
    }
    assert client.deleted == ["review"]
    assert get_profile(apply_env=False).workspace == "default"


def test_workspace_create_refuses_an_environment_owned_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default = _workspace("default")
    client = _WorkspaceClient(selected=default, workspaces=[default])
    _profile(default.name)
    monkeypatch.setenv("LAZYCLOUD_WORKSPACE", "environment")
    reset_settings_cache()
    monkeypatch.setattr(workspace_commands, "workspace_client", lambda: client)

    result = CliRunner().invoke(
        build_public_cli(),
        ["--json", "workspace", "create", "review"],
    )

    assert result.exit_code == 1
    assert client.workspaces == [default]
