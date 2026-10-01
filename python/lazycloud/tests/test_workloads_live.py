"""Pods, devboxes, sandboxes, shells and SSH against a running platform.

Set LAZYCLOUD_ENDPOINT, LAZYCLOUD_WORKSPACE and LAZYCLOUD_TOKEN, for example
to the stack `deploy/local/run.sh start` prints. The suite isolates the
environment per test, so the settings are read once at import. Every test
deletes the deployments, containers and disks it creates.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, TypeVar
from uuid import UUID

import httpx
import lazycloud.config
import pytest
from lazycloud.abstractions.disk import DiskOperationError
from lazycloud.abstractions.shell import Shell
from lazycloud.cli.main import build_public_cli
from lazycloud.clients.api import ApiError
from lazycloud.control import resolve_control_client_config, workloads_client
from lazycloud.session.ssh import current_cli_command
from lazycloud.terminal_shell import InteractiveShell
from shared.api import ContainerState, ErrorCode
from typer.testing import CliRunner, Result

from lazycloud import Disk

_SETTINGS = {
    name: os.environ.get(name, "")
    for name in ("LAZYCLOUD_ENDPOINT", "LAZYCLOUD_WORKSPACE", "LAZYCLOUD_TOKEN")
}
pytestmark = [
    pytest.mark.skipif(
        not _SETTINGS["LAZYCLOUD_ENDPOINT"],
        reason="LAZYCLOUD_ENDPOINT is unset; these tests need a running platform",
    ),
    pytest.mark.usefixtures("live", "isolated_imports"),
]
T = TypeVar("T")
# Cold starts include an image build and pull.
_START_SECONDS = 600.0


@pytest.fixture
def live(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name, value in _SETTINGS.items():
        monkeypatch.setenv(name, value)
    lazycloud.config.reset_settings_cache()
    yield
    lazycloud.config.reset_settings_cache()


def _cli(*args: str) -> Result:
    return CliRunner().invoke(build_public_cli(), list(args))


def _project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str) -> ModuleType:
    name = f"live_{uuid.uuid4().hex[:8]}"
    (tmp_path / f"{name}.py").write_text(source, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])
    return importlib.import_module(name)


def _eventually(check: Callable[[], T | None], *, seconds: float = _START_SECONDS) -> T:
    deadline = time.monotonic() + seconds
    while True:
        value = check()
        if value is not None:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"condition not met within {seconds:g} seconds")
        time.sleep(2)


def _until_free(action: Callable[[], T], *, seconds: float = 120.0) -> T:
    """Retry while a stopping container still holds the resource."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            return action()
        except (ApiError, DiskOperationError) as exc:
            api = exc if isinstance(exc, ApiError) else exc.__cause__
            if (
                not isinstance(api, ApiError)
                or api.code is not ErrorCode.conflict
                or time.monotonic() > deadline
            ):
                raise
        time.sleep(2)


@dataclass
class ScriptedTerminal:
    """Types a script into the shell and keeps what the shell prints."""

    script: list[bytes]
    written: bytearray = field(default_factory=bytearray)
    resized: bool = True

    @property
    def term(self) -> str:
        return "xterm"

    @property
    def signal_exit_code(self) -> int | None:
        return None

    def activate(self) -> AbstractContextManager[None]:
        return nullcontext()

    def size(self) -> tuple[int, int]:
        return (120, 40)

    def read(self, timeout_seconds: float) -> bytes | None:
        time.sleep(timeout_seconds)
        return self.script.pop(0) if self.script else None

    def write(self, data: bytes) -> None:
        self.written.extend(data)

    def take_resize(self) -> bool:
        resized, self.resized = self.resized, False
        return resized


POD_APP = """\
import lazycloud

app = lazycloud.App("{app}")

web = app.pod(
    name="web",
    command=["python", "-m", "http.server", "8080"],
    ports={{"http": 8080}},
    keep_warm=60,
)
"""


def test_a_pod_deploys_answers_on_its_url_scales_and_starts_instances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = f"pods_{uuid.uuid4().hex[:8]}"
    module = _project(tmp_path, monkeypatch, POD_APP.format(app=app))
    web = module.web
    client = workloads_client(resolve_control_client_config(timeout_seconds=60))
    try:
        deployment = web.deploy()
        (release,) = deployment.releases
        assert release.url, "a pod with ports answers on a URL"

        def answered() -> int | None:
            try:
                response = httpx.get(release.url, timeout=30)
            except httpx.HTTPError:
                return None
            return response.status_code if response.status_code == 200 else None

        assert _eventually(answered) == 200

        scaled = web.scale(2)
        assert scaled.scaling is not None
        assert scaled.scaling.min_containers == 2

        def two_ready() -> bool | None:
            page = client.api.list_containers(client.workspace, live=True, deployment=scaled.id)
            ready = [c for c in page.containers if c.state is ContainerState.ready]
            return True if len(ready) >= 2 else None

        _eventually(two_ready)

        instance = web.create(timeout_seconds=120)
        ready = client.connect(UUID(instance.container_id), wait_seconds=60)
        assert ready.state is ContainerState.ready
        assert instance.terminate() is True

        def stopped() -> bool | None:
            container = client.api.get_container(client.workspace, UUID(instance.container_id))
            return True if container.state is ContainerState.stopped else None

        _eventually(stopped, seconds=120)

        terminal = ScriptedTerminal([b"echo shell-$((6*7))\n", b"exit 7\n"])
        shell = Shell(interactive_shell=InteractiveShell(terminal=terminal))
        session = shell.create_standalone(web.stub_id)
        assert shell.connect(session) == 7
        assert b"shell-42" in terminal.written
    finally:
        web.delete()


SANDBOX_APP = """\
import lazycloud

app = lazycloud.App("{app}")

box = app.sandbox(name="scratch", keep_warm_seconds=300)
"""


def test_a_sandbox_runs_processes_and_files_and_controls_its_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = f"sandboxes_{uuid.uuid4().hex[:8]}"
    module = _project(tmp_path, monkeypatch, SANDBOX_APP.format(app=app))
    sandbox = module.box.create(timeout_seconds=_START_SECONDS)
    try:
        result = sandbox.run("echo hello", timeout_seconds=60)
        assert (result.exit_code, result.stdout) == (0, "hello\n")
        assert sandbox.run(["sh", "-c", "exit 3"]).exit_code == 3

        sleeper = sandbox.process.exec("sleep", "300")
        assert sleeper.pid in {item.pid for item in sandbox.list_processes()}
        sleeper.kill()
        assert sleeper.wait(timeout=30) == 128 + 15

        local = tmp_path / "upload.txt"
        local.write_text("alpha beta\nbeta\n", encoding="utf-8")
        fs = sandbox.fs
        fs.create_directory("data/nested")
        fs.upload_file(local, "data/notes.txt")
        assert fs.stat_file("data/notes.txt").size == local.stat().st_size
        assert {item.name for item in fs.list_files("data")} == {"nested", "notes.txt"}
        found = fs.find_in_files("data", "beta")
        assert [(r.path.rsplit("/", 1)[-1], len(r.matches)) for r in found] == [("notes.txt", 2)]
        fs.replace_in_files("data", "beta", "gamma")
        fs.download_file("data/notes.txt", tmp_path / "back.txt")
        assert (tmp_path / "back.txt").read_text(encoding="utf-8") == "alpha gamma\ngamma\n"
        fs.delete_file("data/notes.txt")
        fs.delete_directory("data")
        assert "data" not in {item.name for item in fs.list_files(".")}

        sandbox.process.exec("python3", "-m", "http.server", "8000")
        url = sandbox.expose_port(8000)
        assert sandbox.list_urls() == {8000: url}
        assert _eventually(lambda: _status(url), seconds=120) == 200

        policy = sandbox.update_network_permissions(block_network=True)
        assert policy.block_network is True
        blocked = sandbox.run(
            [
                "python3",
                "-c",
                "import urllib.request; urllib.request.urlopen('https://1.1.1.1', timeout=5)",
            ]
        )
        assert blocked.exit_code != 0
        assert sandbox.network_permissions().block_network is True
        assert sandbox.update_network_permissions().block_network is False

        sandbox.update_ttl(120)
    finally:
        assert sandbox.terminate() is True


def _status(url: str) -> int | None:
    try:
        response = httpx.get(url, timeout=30)
    except httpx.HTTPError:
        return None
    return response.status_code if response.status_code == 200 else None


DEVBOX_APP = """\
import lazycloud

app = lazycloud.App("{app}")

box = app.devbox(
    "{name}",
    image=lazycloud.Image(),
    disk="1Gi",
    cpu=1,
    memory="1Gi",
    agent_harnesses=[],
    keep_warm=120,
)
"""


@pytest.mark.skipif(shutil.which("ssh") is None, reason="needs an OpenSSH client")
def test_ssh_config_makes_plain_ssh_reach_a_devbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suffix = uuid.uuid4().hex[:8]
    app, name = f"devboxes_{suffix}", f"box-{suffix}"
    module = _project(tmp_path, monkeypatch, DEVBOX_APP.format(app=app, name=name))
    monkeypatch.setenv("HOME", str(tmp_path))
    try:
        module.box.deploy()

        configured = _cli("--json", "ssh-config", name, "--app", app)
        assert configured.exit_code == 0, configured.output
        payload: dict[str, Any] = json.loads(configured.stdout)
        (alias,) = payload["hosts"]
        ssh = subprocess.run(
            ["ssh", "-F", payload["config"], "-o", "BatchMode=yes", alias, "true"],
            capture_output=True,
            text=True,
            timeout=_START_SECONDS,
            env=os.environ.copy(),
        )
        assert ssh.returncode == 0, ssh.stderr

        # `devbox ssh` reaches it through the hidden ssh-proxy and ssh-cert.
        devbox_ssh = subprocess.run(
            [
                *current_cli_command(),
                "devbox",
                name,
                "ssh",
                "--app",
                app,
                "--",
                "-o",
                "BatchMode=yes",
                "echo",
                "devbox-ok",
            ],
            capture_output=True,
            text=True,
            timeout=_START_SECONDS,
            env=os.environ.copy(),
        )
        assert (devbox_ssh.returncode, devbox_ssh.stdout.strip()) == (0, "devbox-ok"), (
            devbox_ssh.stderr
        )

        status = _cli("--json", "devbox", name, "status", "--app", app)
        assert status.exit_code == 0, status.output
        assert json.loads(status.stdout)["name"] == name
    finally:
        module.box.delete()
        _until_free(lambda: Disk(name).delete())
