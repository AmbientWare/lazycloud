from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
import typer
from lazycloud.cli.main import build_public_cli
from lazycloud.cli.main import start as client_start
from lazycloud.cli.volumes import parse_remote_path, parse_remote_path_if_schemed
from shared.containers import ContainerStatus
from shared.http.compute import (
    ContainerResponse,
    ContainerWithAppPageResponse,
    ContainerWithAppResponse,
    MachineJoinCommandRequest,
    MachineJoinCommandResponse,
)
from shared.http.gateway import AttachToContainerResponse
from shared.http.secrets import GetSecretResponse, SecretWireRecord
from shared.http.tasks import TaskPageResponse, TaskResponse
from typer._click.core import Command
from typer.core import TyperGroup
from typer.main import get_command
from typer.testing import CliRunner

client_cli = build_public_cli()


@dataclass
class FakeMachineJoinComputeClient:
    requests: list[MachineJoinCommandRequest] = field(default_factory=list)
    removed_machine_ids: list[str] = field(default_factory=list)

    def machine_join_command(
        self,
        request: MachineJoinCommandRequest,
    ) -> MachineJoinCommandResponse:
        self.requests.append(request)
        return MachineJoinCommandResponse(
            command=(
                "curl -fsSL https://gateway.example/install/agent "
                "| sh -s -- --gateway https://gateway.example --join-token join-token"
            ),
            expires_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    def remove_machine(self, machine_id: str) -> None:
        self.removed_machine_ids.append(machine_id)


@dataclass
class FakePublicContainerGateway:
    requests: list[tuple[int, str | None]] = field(default_factory=list)

    def list_containers(
        self,
        *,
        limit: int = 100,
        cursor: str | None = None,
        app_id: str | None = None,
        stub_ids: tuple[str, ...] = (),
        status: ContainerStatus | None = None,
    ) -> ContainerWithAppPageResponse:
        del app_id, stub_ids, status
        self.requests.append((limit, cursor))
        start = int(cursor or "0")
        stop = min(start + limit, 205)
        return ContainerWithAppPageResponse(
            data=[
                ContainerWithAppResponse(
                    container=ContainerResponse(
                        id=f"container-{index:03d}",
                        name=f"worker-{index:03d}",
                        image="python:3.12",
                        command=["python", "worker.py"],
                        workspace_id="workspace-1",
                        status=ContainerStatus.Running,
                        created_at=datetime(2026, 7, 12, tzinfo=UTC),
                    )
                )
                for index in range(start, stop)
            ],
            next=str(stop) if stop < 205 else "",
        )

    def attach_to_container_events(
        self,
        container_id: str,
        *,
        poll_interval_seconds: float = 0.25,
    ) -> Iterator[AttachToContainerResponse]:
        _ = container_id, poll_interval_seconds
        yield AttachToContainerResponse(output="first\n")
        yield AttachToContainerResponse(output="second\n")
        yield AttachToContainerResponse(done=True, exit_code=7)


@dataclass
class FakeTaskResourceClient:
    app_ids: list[str | None] = field(default_factory=list)

    def list_tasks(
        self,
        *,
        limit: int = 100,
        app_id: str | None = None,
    ) -> TaskPageResponse:
        assert limit == 100
        self.app_ids.append(app_id)
        return TaskPageResponse(
            data=[
                TaskResponse(
                    id="task-1",
                    name="probe",
                    app_id=app_id,
                    created_at=datetime(2026, 7, 12, tzinfo=UTC),
                )
            ]
        )


def test_task_list_filters_by_exact_app_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = FakeTaskResourceClient()

    def fake_resource_client(
        *,
        workspace: str | None = None,
    ) -> FakeTaskResourceClient:
        assert workspace == "team"
        return resources

    monkeypatch.setattr("lazycloud.cli.resources.resource_client", fake_resource_client)

    result = CliRunner().invoke(
        client_cli,
        [
            "--json",
            "task",
            "list",
            "--app-id",
            "app-1",
            "--workspace",
            "team",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)[0]["id"] == "task-1"
    assert resources.app_ids == ["app-1"]


def test_public_cli_opens_an_existing_container_shell_without_a_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str | None]] = []

    def open_public_shell(
        _ctx: typer.Context,
        *,
        container_id: str,
        sync_dir: str | None = None,
        workspace: str | None = None,
    ) -> None:
        assert sync_dir is None
        calls.append((container_id, workspace))

    monkeypatch.setattr("lazycloud.cli.execution.open_existing_shell", open_public_shell)

    public_result = CliRunner().invoke(
        client_cli,
        ["shell", "--container-id", "container-1", "--workspace", "team"],
    )

    assert public_result.exit_code == 0, public_result.output
    assert calls == [("container-1", "team")]


def test_interactive_shell_rejects_json_output_before_creating_a_session() -> None:
    result = CliRunner().invoke(
        client_cli,
        ["--json", "shell", "--container-id", "container-1"],
    )

    assert result.exit_code != 0
    assert "--json cannot be used with an interactive shell" in result.output


def test_secret_show_masks_secret_value_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = SecretWireRecord(
        id="API_KEY",
        name="API_KEY",
        value="visible-fixture-value",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 2, tzinfo=UTC),
    )

    class FakeSecretClient:
        def get(self, name: str) -> GetSecretResponse:
            assert name == "API_KEY"
            return GetSecretResponse(secret=secret)

    def fake_secret_client(**_: object) -> FakeSecretClient:
        return FakeSecretClient()

    monkeypatch.setattr("lazycloud.cli.secrets.secret_client", fake_secret_client)

    masked = CliRunner().invoke(client_cli, ["secret", "show", "API_KEY"])
    revealed = CliRunner().invoke(client_cli, ["secret", "show", "API_KEY", "--reveal"])

    assert masked.exit_code == 0
    assert "********" in masked.stdout
    assert "visible-fixture-value" not in masked.stdout
    assert revealed.exit_code == 0
    assert "visible-fixture-value" in revealed.stdout


def test_volume_remote_path_parser_supports_plain_and_scheme_syntax() -> None:
    plain = parse_remote_path("myvol/subdir/file.txt")
    schemed = parse_remote_path_if_schemed("lazycloud://myvol/subdir/file.txt")

    assert plain is not None
    assert plain.volume_name == "myvol"
    assert plain.relative_path == "subdir/file.txt"
    assert plain.full_path == "myvol/subdir/file.txt"
    assert schemed is not None
    assert schemed == plain
    assert parse_remote_path_if_schemed("local/path.txt") is None


@dataclass
class FakeVolumeDeleteClient:
    deleted: list[str] = field(default_factory=list)

    def delete(self, name: str) -> None:
        self.deleted.append(name)


def test_volume_delete_without_tty_requires_yes_flag(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    volumes = FakeVolumeDeleteClient()

    def fake_volume_client(workspace: str | None = None) -> FakeVolumeDeleteClient:
        del workspace
        return volumes

    monkeypatch.setattr("lazycloud.cli.volumes.volume_client", fake_volume_client)

    with pytest.raises(SystemExit) as raised:
        client_start(args=["volume", "delete", "vol-a"], prog_name="lazycloud")

    assert raised.value.code == 1
    captured = capsys.readouterr()
    assert "Confirmation required" in captured.err
    assert "--yes" in captured.err
    assert "[y/N]" not in captured.err
    assert "Aborted" not in captured.err
    assert volumes.deleted == []


def test_volume_delete_without_tty_reports_clean_json_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    volumes = FakeVolumeDeleteClient()

    def fake_volume_client(workspace: str | None = None) -> FakeVolumeDeleteClient:
        del workspace
        return volumes

    monkeypatch.setattr("lazycloud.cli.volumes.volume_client", fake_volume_client)

    with pytest.raises(SystemExit) as raised:
        client_start(args=["--json", "volume", "delete", "vol-a"], prog_name="lazycloud")

    assert raised.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["error"]["type"] == "confirmation_required"
    assert "--yes" in payload["error"]["hint"]
    assert volumes.deleted == []


def test_volume_delete_with_yes_flag_skips_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    volumes = FakeVolumeDeleteClient()

    def fake_volume_client(workspace: str | None = None) -> FakeVolumeDeleteClient:
        del workspace
        return volumes

    monkeypatch.setattr("lazycloud.cli.volumes.volume_client", fake_volume_client)

    result = CliRunner().invoke(client_cli, ["--json", "volume", "delete", "vol-a", "--yes"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"name": "vol-a"}
    assert volumes.deleted == ["vol-a"]


def _command_paths(typer_app: typer.Typer) -> set[str]:
    root = get_command(typer_app)
    paths: set[str] = set()

    def walk(command: Command, prefix: tuple[str, ...] = ()) -> None:
        if not isinstance(command, TyperGroup):
            return
        for name, subcommand in sorted(command.commands.items()):
            path = (*prefix, name)
            paths.add(" ".join(path))
            walk(subcommand, path)

    walk(root)
    return paths
