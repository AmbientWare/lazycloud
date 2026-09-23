from __future__ import annotations

from pathlib import Path

import pytest
from lazycloud.clients.ssh.control import SshControlClient
from lazycloud.session.ssh import SshAccess, SshPaths, SshPodHost, SshSetupError, ssh_host_alias

_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
_OTHER_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEiXkZ0yA7i4zb4bCVI0jQq1h0lJmNfZcBfMmbJ3m5c+"


def _host(app: str, pod: str, key: str) -> SshPodHost:
    return SshPodHost(alias=ssh_host_alias("acme", app, pod), pod=pod, app=app, host_public_key=key)


def test_pods_sharing_a_name_across_apps_get_separate_hosts_and_pins(tmp_path: Path) -> None:
    access = SshAccess(
        client=SshControlClient.from_endpoint("http://127.0.0.1:1"),
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
