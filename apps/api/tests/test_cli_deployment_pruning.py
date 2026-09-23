import json
import socket
from pathlib import Path
from threading import Thread

import pytest
import uvicorn
from api.fastapi_app import create_app
from api.server.services import ApiServices
from identity.auth import AuthService
from lazycloud.cli.main import build_public_cli
from shared.deployment_records import DeploymentSpec
from shared.env import GATEWAY_HTTP_URL_ENV, GATEWAY_TOKEN_ENV
from shared.identity import AuthScope, TokenKind
from typer.testing import CliRunner

pytestmark = pytest.mark.usefixtures("isolated_imports")


def test_cli_combines_files_for_read_only_preview_and_prunes_an_explicit_empty_app(
    isolated_services: ApiServices,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = isolated_services
    app = services.apps.create("cli_prune")
    old = services.deployments.deploy(
        DeploymentSpec(
            name="old",
            handler="pkg:old",
            metadata={"app_id": app.id},
        )
    )
    token, _ = AuthService(services.context).create_token(
        "cli-prune",
        scopes=[AuthScope.Read.value, AuthScope.Write.value],
        kind=TokenKind.Workspace,
        workspace_id=app.workspace_id,
    )
    monkeypatch.chdir(tmp_path)
    (tmp_path / "definition.py").write_text('from lazycloud import App\napp = App("cli_prune")\n')
    (tmp_path / "first.py").write_text(
        'from definition import app\n@app.function(name="first")\ndef first(): return 1\n'
    )
    (tmp_path / "second.py").write_text(
        'from definition import app\n@app.function(name="second")\ndef second(): return 2\n'
    )
    (tmp_path / "empty.py").write_text('from lazycloud import App\napp = App("cli_prune")\n')
    (tmp_path / "invalid.py").write_text(
        'from lazycloud import App, Image\napp = App("invalid_app")\n'
        'image = Image.from_registry("ghcr.io/acme/private", credentials=["CLI_PRUNE_MISSING"])\n'
        "@app.function(image=image)\ndef invalid(): return 1\n"
    )
    monkeypatch.delenv("CLI_PRUNE_MISSING", raising=False)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    endpoint = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(create_app(services), log_level="error"))
    thread = Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    monkeypatch.setenv(GATEWAY_HTTP_URL_ENV, endpoint)
    monkeypatch.setenv(GATEWAY_TOKEN_ENV, token)
    thread.start()
    try:
        runner = CliRunner()
        preview = runner.invoke(
            build_public_cli(),
            [
                "--json",
                "deploy",
                "--diff",
                "-p",
                "first.py",
                "second.py",
                "--workspace",
                app.workspace_id,
            ],
        )
        assert preview.exit_code == 0, preview.output
        payload = json.loads(preview.output)
        assert {(row["name"], row["action"]) for row in payload["data"]} == {
            ("first", "add"),
            ("second", "add"),
            ("old", "remove"),
        }
        assert services.deployments.get(old.id).active
        assert not (tmp_path / ".lazycloudignore").exists()
        failed = runner.invoke(
            build_public_cli(),
            [
                "deploy",
                "-p",
                "empty.py",
                "invalid.py",
                "--workspace",
                app.workspace_id,
            ],
        )
        assert failed.exit_code != 0
        assert "CLI_PRUNE_MISSING" in str(failed.exception)
        assert services.deployments.get(old.id).active
        pruned = runner.invoke(
            build_public_cli(),
            [
                "--json",
                "deploy",
                "-p",
                "empty.py",
                "--workspace",
                app.workspace_id,
            ],
        )
        assert pruned.exit_code == 0, pruned.output
        assert json.loads(pruned.stdout)["pruning"]["removed_versions"] == 1
        assert services.deployments.list(app_id=app.id) == []
        assert services.apps.get(app.id).active
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        assert not thread.is_alive(), "acceptance API did not shut down"
