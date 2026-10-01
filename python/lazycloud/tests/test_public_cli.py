from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import typer
from lazycloud.abstractions.shell import ShellSession
from lazycloud.cli.main import build_public_cli
from lazycloud.cli.main import start as client_start
from lazycloud.cli.volumes import parse_remote_path, parse_remote_path_if_schemed
from shared.http.secrets import GetSecretResponse, SecretWireRecord
from typer.testing import CliRunner

client_cli = build_public_cli()


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

    def default_dev_pod() -> FakePod:
        return FakePod()

    monkeypatch.setattr("lazycloud.cli.development._default_dev_pod", default_dev_pod)
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


@pytest.mark.usefixtures("isolated_imports")
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


@pytest.mark.usefixtures("isolated_imports")
def test_run_displays_python_results_without_json_conversion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "python_results.py").write_text(
        "import numpy as np\n"
        "class Result:\n"
        "    def __repr__(self): return 'PythonResult()'\n"
        "def main():\n"
        "    cycle = []\n"
        "    cycle.append(cycle)\n"
        "    return {'array': np.array([1, 2, 3]), 'object': Result(), 'cycle': cycle}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(client_cli, ["run", "python_results:main"])
    assert result.exit_code == 0, result.output
    assert "array([1, 2, 3])" in result.stdout
    assert "PythonResult()" in result.stdout


@pytest.mark.usefixtures("isolated_imports")
def test_run_json_preserves_values_and_reports_unsupported_results_without_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "json_results.py").write_text(
        "import numpy as np\n"
        "def array(): return np.array([1, 2, 3])\n"
        "def plain(): return {'value': [1, None, 3]}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(client_cli, ["--json", "run", "json_results:plain"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"value": [1, None, 3]}
    with pytest.raises(SystemExit) as raised:
        client_start(args=["--json", "run", "json_results:array"], prog_name="lazycloud")
    assert raised.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"]["type"] == "result_not_json_serializable"


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


def test_volume_delete_without_tty_requires_yes_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        client_start(args=["volume", "delete", "vol-a"], prog_name="lazycloud")

    assert raised.value.code == 1
    captured = capsys.readouterr()
    assert "Confirmation required" in captured.err
    assert "--yes" in captured.err
    assert "[y/N]" not in captured.err
    assert "Aborted" not in captured.err


def test_volume_delete_without_tty_reports_clean_json_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        client_start(args=["--json", "volume", "delete", "vol-a"], prog_name="lazycloud")

    assert raised.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["error"]["type"] == "confirmation_required"
    assert "--yes" in payload["error"]["hint"]
