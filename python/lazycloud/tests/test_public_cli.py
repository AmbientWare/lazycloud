from __future__ import annotations

import json
from pathlib import Path

import pytest
from lazycloud.cli.main import build_public_cli
from lazycloud.cli.main import start as client_start
from typer.testing import CliRunner

from tests.api_server import ApiRequest, FakeApi, Reply, json_reply

client_cli = build_public_cli()


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


def test_ls_reads_plain_and_scheme_volume_paths_and_follows_pages(fake_api: FakeApi) -> None:
    files = "/v1/workspaces/team/volumes/myvol/files"

    @fake_api.route("GET", files)
    def listing(request: ApiRequest) -> Reply:
        entry: dict[str, object] = {"path": "subdir/a.txt", "is_dir": False, "size_bytes": 3}
        if "cursor" in request.query:
            second: dict[str, object] = {**entry, "path": "subdir/b.txt"}
            return json_reply({"files": [second]})
        return json_reply({"files": [entry], "next_cursor": "c2"})

    plain = CliRunner().invoke(client_cli, ["--json", "ls", "myvol/subdir"])
    schemed = CliRunner().invoke(client_cli, ["--json", "ls", "lazycloud://myvol/subdir"])

    assert plain.exit_code == 0, plain.output
    assert [item["path"] for item in json.loads(plain.stdout)] == ["subdir/a.txt", "subdir/b.txt"]
    assert schemed.stdout == plain.stdout
    assert {tuple(r.query["path"]) for r in fake_api.calls("GET", files)} == {("subdir",)}


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
