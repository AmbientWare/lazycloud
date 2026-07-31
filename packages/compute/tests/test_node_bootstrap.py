from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from compute.node_bootstrap import (
    NodeBootstrapProfile,
    NodeBootstrapSettings,
    node_bootstrap_script,
)
from pydantic import SecretStr

_PEER = "control-plane.tailnet-example.ts.net"

# The smallest fragment that satisfies the provider seam, standing in for a
# cloud's metadata service and identity proof.
_TEST_IDENTITY_SHELL = """
NODE_ID="node-0123456789abcdef0"
PROVIDER_INSTALL_FLAGS=(--provider test)

resolve_node_identity() {
  :
}

report_identity_fields() {
  printf ',"provider":"test","provider_instance_id":"%s"' "$NODE_ID"
}

node_fingerprint() {
  printf '%s' "$NODE_ID"
}

node_hostname() {
  printf '%s' "$NODE_ID"
}
"""


def _script() -> str:
    return node_bootstrap_script(
        NodeBootstrapSettings(
            control_plane_url=f"http://{_PEER}:9000",
            enrollment_request_id="12345678-1234-4123-8123-123456789abc",
            agent_binary_url=f"https://artifacts.example.com/agent/{'a' * 64}/agent",
            agent_sha256="a" * 64,
            worker_image_digest=f"registry.example.com/worker@sha256:{'b' * 64}",
            tailnet_auth_key=SecretStr("tskey-auth-poolbootstrap"),
        ),
        NodeBootstrapProfile(provider="test", identity_shell=_TEST_IDENTITY_SHELL),
    )


def _driver(tmp_path: Path, *, resolvable: bool) -> Path:
    """The generated script with tailscale replaced, driven through `tailnet_join`."""
    bash = shutil.which("bash")
    if bash is None:
        pytest.fail("bash is required to prove the generated bootstrap script")

    daemon = tmp_path / "fake-tailscaled"
    daemon.write_text("#!/bin/sh\nexec sleep 120\n", encoding="utf-8")
    daemon.chmod(0o755)

    lines = _script().rstrip("\n").split("\n")
    assert lines[-1] == "bootstrap_main"
    resolved = 'printf "100.64.0.9\\n"' if resolvable else "return 1"
    driver = tmp_path / f"join-{resolvable}.sh"
    driver.write_text(
        "\n".join(
            [
                *lines[:-1],
                "trap - ERR",
                f'TAILSCALED_BIN="{daemon}"',
                f'TAILNET_STATE_DIR="{tmp_path / "state"}"',
                f'TAILNET_STATE_FILE="{tmp_path / "state" / "tailscaled.state"}"',
                f'TAILNET_SOCKET="{tmp_path / "state" / "tailscaled.sock"}"',
                f'UP_LOG="{tmp_path / "up.log"}"',
                "ts() {",
                '  case "$1" in',
                "    status) return 0 ;;",
                f'    ip) if [ "$3" = "{_PEER}" ]; then {resolved}; else return 1; fi ;;',
                '    up) printf "%s\\n" "$*" >>"$UP_LOG"',
                # Read the key exactly as tailscale does, so the test observes
                # what the daemon would have received.
                '        key_arg="${2#--auth-key=file:}"',
                '        cat "$key_arg" >>"$UP_LOG" ;;',
                "  esac",
                "}",
                # The generated script checks that the agent will be able
                # to resolve the same name it just pinned.
                'getent() { [ "$2" = "control-plane.tailnet-example.ts.net" ]; }',
                "tailnet_join",
                'printf "RESOLVE=%s\\n" "${CURL_RESOLVE[*]}"',
                "tailnet_stop",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return driver


def test_the_bootstrap_pins_the_control_plane_peer_without_exposing_its_key(
    tmp_path: Path,
) -> None:
    """The node joins, pins the peer's address, and leaves no key behind.

    The key reaches tailscale through a file because a command line is readable
    by every process on the machine, and the machine runs user workloads
    minutes later. The address is pinned rather than substituted into the URL so
    the request still names the peer it was configured with — the same rule the
    platform's internal HTTP client follows, and the one that keeps a signed
    object-store URL valid.
    """
    bash = shutil.which("bash")
    if bash is None:
        pytest.fail("bash is required to prove the generated bootstrap script")
    driver = _driver(tmp_path, resolvable=True)

    run = subprocess.run([bash, driver.as_posix()], capture_output=True, text=True, check=False)

    assert run.returncode == 0, run.stderr
    up_log = (tmp_path / "up.log").read_text(encoding="utf-8")
    assert "--hostname=bootstrap-node-0123456789abcdef0" in up_log
    # The key was delivered, and never as an argument.
    assert "tskey-auth-poolbootstrap" in up_log
    assert "--auth-key=tskey" not in up_log
    assert not list((tmp_path / "state").glob("bootstrap.key"))
    assert f"RESOLVE=--resolve {_PEER}:9000:100.64.0.9" in run.stdout


def test_a_control_plane_that_is_not_a_peer_stops_the_boot_and_names_it(
    tmp_path: Path,
) -> None:
    """Continuing would produce a node that reports healthy and reaches nothing.

    Every later call in the boot addresses the control plane over the tailnet,
    so a name that resolves to no peer has to fail here rather than as a
    timeout attributed to whatever ran next.
    """
    bash = shutil.which("bash")
    if bash is None:
        pytest.fail("bash is required to prove the generated bootstrap script")
    driver = _driver(tmp_path, resolvable=False)

    run = subprocess.run([bash, driver.as_posix()], capture_output=True, text=True, check=False)

    assert run.returncode != 0
    assert _PEER in run.stderr
    assert "not a reachable tailnet peer" in run.stderr
