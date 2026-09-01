from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
import typer
from lazycloud.abstractions.shell import ShellSession
from lazycloud.cli.main import build_public_cli
from lazycloud.cli.main import start as client_start
from lazycloud.cli.volumes import parse_remote_path, parse_remote_path_if_schemed
from shared.http.secrets import GetSecretResponse, SecretWireRecord
from shared.http.tasks import TaskPageResponse, TaskResponse
from typer.testing import CliRunner

client_cli = build_public_cli()


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
            "--app",
            "11111111-1111-4111-8111-111111111111",
            "--workspace",
            "team",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)[0]["id"] == "task-1"
    assert resources.app_ids == ["11111111-1111-4111-8111-111111111111"]


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


def test_development_session_connects_without_printing_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = ShellSession(
        container_id="container-1",
        stub_id="stub-1",
        username="shell",
        password="fixture-shell-secret",
    )
    opened: list[ShellSession] = []

    class FakePod:
        workspace: str | None = None

        def shell(self, *, workspace: str | None, sync_dir: str) -> ShellSession:
            assert workspace == "team"
            assert sync_dir == "./"
            return session

    def open_session(
        _ctx: typer.Context,
        selected: ShellSession,
        *,
        workspace: str | None = None,
    ) -> None:
        assert workspace == "team"
        opened.append(selected)

    monkeypatch.setattr("lazycloud.cli.development._default_dev_pod", lambda _overrides: FakePod())
    monkeypatch.setattr("lazycloud.cli.development.open_shell_session", open_session)

    result = CliRunner().invoke(client_cli, ["dev", "--workspace", "team"])

    assert result.exit_code == 0, result.output
    assert opened == [session]
    assert "fixture-shell-secret" not in result.output


def test_interactive_shell_rejects_json_output_before_creating_a_session() -> None:
    result = CliRunner().invoke(
        client_cli,
        ["--json", "shell", "--container-id", "container-1"],
    )

    assert result.exit_code != 0
    assert "--json cannot be used with an interactive shell" in result.output


def test_public_entrypoint_formats_usage_errors_as_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        client_start(args=["--json", "does-not-exist"], prog_name="lazycloud")

    assert raised.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["error"]["type"] == "invalid_usage"
    assert "does-not-exist" in payload["error"]["message"]


def test_handler_argument_named_json_does_not_enable_machine_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = tmp_path / "handler_args.py"
    module.write_text(
        "def inspect(*args):\n    raise RuntimeError(f'handler args: {args!r}')\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as raised:
        client_start(
            args=["run", "handler_args:inspect", "--", "--json"],
            prog_name="lazycloud",
        )

    assert raised.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Unexpected error" in captured.err
    assert "handler args" in captured.err


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
