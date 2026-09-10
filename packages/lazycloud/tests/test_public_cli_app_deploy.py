from __future__ import annotations

import sys
from pathlib import Path

import pytest
from lazycloud.cli.handler_workflows import HandlerLoadError, load_deployment_object
from lazycloud.cli.main import build_public_cli
from typer.testing import CliRunner

from lazycloud import App

pytestmark = pytest.mark.usefixtures("isolated_imports")


def test_file_deploy_selects_the_whole_app_and_deduplicates_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])
    (tmp_path / "app_definition.py").write_text(
        'from lazycloud import App\napp = App("example")\npod = app.pod(name="worker")\n',
        encoding="utf-8",
    )
    (tmp_path / "workloads.py").write_text(
        "from app_definition import app\nalias = app\n@app.function()\ndef hello(): return 1\n",
        encoding="utf-8",
    )
    try:
        app = load_deployment_object("workloads.py")
        assert isinstance(app, App)
        assert app is load_deployment_object("workloads:app")
        assert [item.spec().name for item in app.resources] == ["worker", "hello"]
        assert load_deployment_object("workloads:hello") is app.resources[1]
        result = CliRunner().invoke(
            build_public_cli(), ["deploy", "workloads.py", "--name", "collision"]
        )
        assert result.exit_code != 0
        assert "--name requires a handler reference or --resource" in result.output
    finally:
        sys.modules.pop("workloads", None)
        sys.modules.pop("app_definition", None)


def test_file_deploy_requires_a_selection_when_several_apps_are_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])
    (tmp_path / "several_apps.py").write_text(
        'from lazycloud import App\nfirst = App("first")\nsecond = App("second")\n',
        encoding="utf-8",
    )
    try:
        result = CliRunner().invoke(build_public_cli(), ["deploy", "several_apps.py"])
        assert result.exit_code != 0
        assert "multiple apps found" in result.output
        assert "several_apps.py:first" in result.output
        assert "several_apps.py:second" in result.output
        app = load_deployment_object("several_apps.py:second")
        assert isinstance(app, App) and app.slug == "second"
    finally:
        sys.modules.pop("several_apps", None)


def test_file_deploy_rejects_a_module_without_an_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])
    (tmp_path / "empty_app.py").write_text("def helper(): return 1\n", encoding="utf-8")
    try:
        with pytest.raises(HandlerLoadError, match="no App found"):
            load_deployment_object("empty_app.py")
    finally:
        sys.modules.pop("empty_app", None)
