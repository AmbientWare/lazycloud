from __future__ import annotations

import sys
from pathlib import Path

import pytest
from lazycloud.cli.handler_workflows import HandlerLoadError, load_deployment_objects
from lazycloud.cli.main import build_public_cli
from typer.testing import CliRunner


def test_file_deploy_selects_local_decorators_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])
    (tmp_path / "imported_workloads.py").write_text(
        'from lazycloud import App\napp = App("imported")\n'
        "@app.function()\ndef foreign(): return 1\n",
        encoding="utf-8",
    )
    (tmp_path / "local_workloads.py").write_text(
        "from lazycloud import App\nfrom imported_workloads import foreign\n"
        'app = App("local")\n'
        '@app.function(cron="0 * * * *")\ndef scheduled(): return 1\n'
        "alias = scheduled\n"
        '@app.endpoint()\ndef endpoint(): return {"ok": True}\n'
        "@app.asgi()\ndef web(): return None\n"
        "@app.realtime()\ndef socket(context, message): return message\n"
        "def helper(): return 2\n",
        encoding="utf-8",
    )
    try:
        targets = load_deployment_objects("local_workloads.py")
        assert [reference for reference, _ in targets] == [
            "local_workloads:scheduled",
            "local_workloads:endpoint",
            "local_workloads:web",
            "local_workloads:socket",
        ]
        assert load_deployment_objects("local_workloads:scheduled")[0][1] is targets[0][1]
        result = CliRunner().invoke(
            build_public_cli(), ["deploy", "local_workloads.py", "--name", "collision"]
        )
        assert result.exit_code != 0
        assert "--name requires a single handler" in result.output
    finally:
        sys.modules.pop("local_workloads", None)
        sys.modules.pop("imported_workloads", None)


def test_file_deploy_rejects_a_module_without_decorated_functions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])
    (tmp_path / "empty_workloads.py").write_text("def helper(): return 1\n", encoding="utf-8")
    try:
        with pytest.raises(HandlerLoadError, match="no deployable decorated functions"):
            load_deployment_objects("empty_workloads.py")
    finally:
        sys.modules.pop("empty_workloads", None)
