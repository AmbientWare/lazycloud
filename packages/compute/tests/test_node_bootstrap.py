from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from compute.node_bootstrap import (
    TAILNET_SERVICE_NAME,
    TAILNET_SOCKET_PATH,
    TAILNET_STATE_FILE,
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


def _script(control_plane_url: str = f"http://{_PEER}:9000") -> str:
    return node_bootstrap_script(
        NodeBootstrapSettings(
            control_plane_url=control_plane_url,
            enrollment_request_id="12345678-1234-4123-8123-123456789abc",
            agent_binary_url=f"https://artifacts.example.com/agent/{'a' * 64}/agent",
            agent_sha256="a" * 64,
            worker_image_digest=f"registry.example.com/worker@sha256:{'b' * 64}",
            tailnet_auth_key=SecretStr("tskey-auth-poolbootstrap"),
        ),
        NodeBootstrapProfile(provider="test", identity_shell=_TEST_IDENTITY_SHELL),
    )


def _require_bash() -> str:
    bash = shutil.which("bash")
    if bash is None:
        pytest.fail("bash is required to prove the generated bootstrap script")
    return bash


def _stubs(tmp_path: Path) -> Path:
    """A PATH directory standing in for systemd, tailscaled, and the agent.

    `systemctl restart` really starts the unit's `ExecStart`, so the daemon this
    script installs is a live process for the rest of the run — which is the
    only way to observe whether it is still running when the agent takes over.
    """
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()

    (stub_dir / "fake-tailscaled").write_text("#!/bin/sh\nexec sleep 120\n", encoding="utf-8")
    (stub_dir / "systemctl").write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  restart)\n"
        f'    unit="{tmp_path}/unit/$2"\n'
        "    cmd=$(sed -n 's/^ExecStart=//p' \"$unit\")\n"
        f'    printf "%s\\n" "$cmd" > "{tmp_path}/execstart.txt"\n'
        "    $cmd >/dev/null 2>&1 &\n"
        f'    printf "%s" "$!" > "{tmp_path}/daemon.pid"\n'
        "    ;;\n"
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    # Records its argv and, crucially, whether the tailnet daemon is still up at
    # the moment the agent takes over.
    (stub_dir / "fake-agent").write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" > "{tmp_path}/install-service.txt"\n'
        # The real agent persists its identity here once it enrolls, which is
        # what tells the script it may stop watching the unit.
        f'mkdir -p "{tmp_path}/agent" && printf "{{}}" > "{tmp_path}/agent/agent-state.json"\n'
        f'if kill -0 "$(cat {tmp_path}/daemon.pid)" 2>/dev/null; then\n'
        f'  printf "alive" > "{tmp_path}/daemon-at-handoff.txt"\n'
        "else\n"
        f'  printf "dead" > "{tmp_path}/daemon-at-handoff.txt"\n'
        "fi\n",
        encoding="utf-8",
    )
    for name in ("fake-tailscaled", "systemctl", "fake-agent"):
        (stub_dir / name).chmod(0o755)
    return stub_dir


def _run_bootstrap(
    tmp_path: Path,
    *,
    resolvable: bool = True,
    publicly_resolvable: bool = True,
    control_plane_url: str = f"http://{_PEER}:9000",
) -> subprocess.CompletedProcess[str]:
    bash = _require_bash()
    stub_dir = _stubs(tmp_path)
    unit_dir = tmp_path / "unit"
    unit_dir.mkdir()

    lines = _script(control_plane_url).rstrip("\n").split("\n")
    assert lines[-1] == "bootstrap_main"
    resolved = 'printf "100.64.0.9\\n"' if resolvable else "return 1"
    driver = tmp_path / f"bootstrap-{resolvable}-{publicly_resolvable}.sh"
    driver.write_text(
        "\n".join(
            [
                *lines[:-1],
                f'TAILSCALED_BIN="{stub_dir / "fake-tailscaled"}"',
                f'AGENT_BIN="{stub_dir / "fake-agent"}"',
                f'AGENT_STATE_DIR="{tmp_path / "agent"}"',
                f'TAILNET_STATE_DIR="{tmp_path / "state"}"',
                f'TAILNET_STATE_FILE="{tmp_path / "state" / "tailscaled.state"}"',
                f'TAILNET_SOCKET="{tmp_path / "state" / "tailscaled.sock"}"',
                f'TAILNET_SERVICE_PATH="{unit_dir / TAILNET_SERVICE_NAME}"',
                f'PATH="{stub_dir}:$PATH"',
                f'UP_LOG="{tmp_path / "up.log"}"',
                "ts() {",
                '  case "$1" in',
                "    status) return 0 ;;",
                f'    ip) if [ "$3" = "{_PEER}" ]; then {resolved}; else return 1; fi ;;',
                '    up) printf "%s\\n" "$*" >>"$UP_LOG"',
                '        key_arg="${2#--auth-key=file:}"',
                '        cat "$key_arg" >>"$UP_LOG" ;;',
                "  esac",
                "}",
                "getent() { " + ("true" if publicly_resolvable else "false") + "; }",
                # Docker, the agent artifact, and the Tailscale download are
                # proven elsewhere; this run is about the daemon's lifetime.
                "ensure_docker() { :; }",
                "ensure_agent() { :; }",
                "ensure_tailscale() { :; }",
                "curl() { return 0; }",
                "bootstrap_main",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return subprocess.run([bash, driver.as_posix()], capture_output=True, text=True, check=False)


def test_the_tailnet_daemon_is_still_running_when_the_agent_takes_over(
    tmp_path: Path,
) -> None:
    """The node keeps one tailnet session from boot until it is terminated.

    An earlier version stopped the daemon before `install-service` so the agent
    could start its own. The pool key is ephemeral, so stopping it made the
    control server delete the device: the agent came up holding a revoked key
    and enrolled over a tailnet it was no longer on, which looked like silence
    rather than a failure. Nothing may stop the daemon between join and handoff.
    """
    run = _run_bootstrap(tmp_path)

    assert run.returncode == 0, run.stderr
    assert (tmp_path / "daemon-at-handoff.txt").read_text(encoding="utf-8") == "alive"

    flags = (tmp_path / "install-service.txt").read_text(encoding="utf-8")
    assert "--tailnet-mode sidecar" in flags
    assert f"--tailnet-socket {tmp_path / 'state' / 'tailscaled.sock'}" in flags


def test_the_unit_runs_the_daemon_the_agent_is_told_to_dial(tmp_path: Path) -> None:
    """systemd rejects a relative ExecStart, and the agent dials by path.

    Both fail the same way — a node that installs Tailscale, never gets on the
    tailnet, and reports nothing — so both are pinned here rather than
    discovered on an instance.
    """
    _run_bootstrap(tmp_path)

    exec_start = (tmp_path / "execstart.txt").read_text(encoding="utf-8").strip()
    binary = exec_start.split(" ", 1)[0]
    assert binary.startswith("/"), exec_start
    assert f"--socket={tmp_path / 'state' / 'tailscaled.sock'}" in exec_start
    assert f"--state={tmp_path / 'state' / 'tailscaled.state'}" in exec_start


def test_the_generated_script_never_stops_the_daemon() -> None:
    """The teardown is gone, not merely unused."""
    script = _script()

    assert "tailnet_stop" not in script
    assert "TAILSCALED_PID" not in script
    # The socket and state the unit serves are the ones the agent is handed.
    assert TAILNET_SOCKET_PATH in script
    assert TAILNET_STATE_FILE in script


def test_a_control_plane_that_resolves_no_way_at_all_stops_the_boot_and_names_it(
    tmp_path: Path,
) -> None:
    """Continuing would produce a node that reports healthy and reaches nothing.

    This step runs before the first report, so a failure here is invisible to
    the control plane and arrives as `bootstrap_timed_out`, which names nothing.
    Refusing with the host in the message is the only attributable outcome.
    """
    run = _run_bootstrap(tmp_path, resolvable=False, publicly_resolvable=False)

    assert run.returncode != 0
    assert _PEER in run.stderr
    assert "does not resolve" in run.stderr


def test_a_public_control_plane_origin_boots_without_being_a_tailnet_peer(
    tmp_path: Path,
) -> None:
    """Managed nodes are handed the public origin, which no peer lookup answers.

    Requiring a peer refused every such node at a step that cannot report, so
    the machine died as an unexplained timeout. The node still joins the tailnet
    here — that is the data path — but reaching the control plane must not
    depend on it.
    """
    run = _run_bootstrap(
        tmp_path,
        resolvable=False,
        control_plane_url="https://lazycloud.example",
    )

    assert run.returncode == 0, run.stderr


def test_a_control_plane_reached_at_its_tailnet_address_needs_no_resolver(
    tmp_path: Path,
) -> None:
    """A deployment may point nodes at the control plane's tailnet address.

    The name path asks `tailscale ip` for the peer and then checks MagicDNS on
    behalf of the agent. Neither applies to an address that is already the
    answer, and running them anyway refused the boot as `network_join_failed`
    on a node that was in fact on the tailnet and reporting.
    """
    run = _run_bootstrap(tmp_path, control_plane_url="http://100.79.134.95:9000")

    assert run.returncode == 0, run.stderr
    assert (tmp_path / "daemon-at-handoff.txt").read_text(encoding="utf-8") == "alive"


def test_the_bootstrap_delivers_its_key_without_exposing_it(tmp_path: Path) -> None:
    """A command line is readable by every process; this machine runs user work.

    The key reaches tailscale through a 0600 file, and the file is removed once
    the join returns.
    """
    _run_bootstrap(tmp_path)

    up_log = (tmp_path / "up.log").read_text(encoding="utf-8")
    assert "--hostname=bootstrap-node-0123456789abcdef0" in up_log
    assert "tskey-auth-poolbootstrap" in up_log
    assert "--auth-key=tskey" not in up_log
    assert not list((tmp_path / "state").glob("bootstrap.key"))
