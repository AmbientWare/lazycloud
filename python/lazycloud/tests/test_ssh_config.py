from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from lazycloud.clients.api import ApiClient
from lazycloud.clients.workloads import WorkloadsClient
from lazycloud.session.ssh import (
    SshAccess,
    SshPaths,
    SshPodHost,
    SshSetupError,
    current_cli_command,
)

_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
_OTHER_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEiXkZ0yA7i4zb4bCVI0jQq1h0lJmNfZcBfMmbJ3m5c+"


def _host(app: str, pod: str, key: str) -> SshPodHost:
    return SshPodHost(alias=f"lazycloud-acme-{app}-{pod}", pod=pod, app=app, host_public_key=key)


def test_pods_sharing_a_name_across_apps_get_separate_hosts_and_pins(tmp_path: Path) -> None:
    access = SshAccess(
        client=WorkloadsClient(ApiClient(endpoint="http://127.0.0.1:1"), "acme"),
        workspace="acme",
        paths=SshPaths(root=tmp_path),
        cli_command=("lazycloud",),
    )

    access.write_hosts([_host("dev", "box", _KEY), _host("ci", "box", _OTHER_KEY)])

    assert sorted(path.name for path in access.paths.hosts.iterdir()) == [
        "lazycloud-acme-ci-box.conf",
        "lazycloud-acme-dev-box.conf",
    ]
    assert access.paths.known_hosts.read_text().splitlines() == [
        f"lazycloud-acme-dev-box {_KEY}",
        f"lazycloud-acme-ci-box {_OTHER_KEY}",
    ]
    access.write_hosts([_host("dev-box", "x", _KEY)])
    with pytest.raises(SshSetupError, match="already names another pod"):
        access.write_hosts([_host("dev", "box-x", _OTHER_KEY)])


def test_a_full_sync_removes_only_this_workspaces_stale_hosts(tmp_path: Path) -> None:
    paths = SshPaths(root=tmp_path)
    client = WorkloadsClient(ApiClient(endpoint="http://127.0.0.1:1"), "acme")
    acme = SshAccess(client=client, workspace="acme", paths=paths, cli_command=("lazycloud",))
    other = SshAccess(client=client, workspace="other", paths=paths, cli_command=("lazycloud",))
    acme.write_hosts([_host("dev", "box", _KEY), _host("dev", "gone", _OTHER_KEY)])
    other_host = SshPodHost(
        alias="lazycloud-other-dev-box", pod="box", app="dev", host_public_key=_KEY
    )
    other.write_hosts([other_host])
    (paths.hosts / "mine.conf").write_text("Host mine\n    HostName example.test\n")

    removed = acme.sync_hosts([_host("dev", "box", _KEY)])

    assert removed == ["lazycloud-acme-dev-gone"]
    assert sorted(path.name for path in paths.hosts.iterdir()) == [
        "lazycloud-acme-dev-box.conf",
        "lazycloud-other-dev-box.conf",
        "mine.conf",
    ]
    assert "lazycloud-acme-dev-gone" not in paths.known_hosts.read_text()
    assert f"lazycloud-other-dev-box {_KEY}" in paths.known_hosts.read_text().splitlines()


def test_ssh_runs_the_hidden_commands_of_the_running_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An older `lazycloud` first on PATH must not answer for this one.
    stale = tmp_path / "lazycloud"
    stale.write_text("#!/bin/sh\necho \"No such command '$1'\" >&2\nexit 2\n")
    stale.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    for command in ("ssh-proxy", "ssh-cert"):
        done = subprocess.run(
            [*current_cli_command(), command, "--help"], capture_output=True, text=True, check=False
        )
        assert done.returncode == 0, done.stderr
