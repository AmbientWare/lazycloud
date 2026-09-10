from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

from agent.operations import (
    AGENT_RUNTIME_READY_FILE,
    build_agent_install_script,
)
from shared.app_identity import AGENT_NAME
from tests.url_constants import EXAMPLE_URL


def test_install_script_runs_foreground_external_agent_without_leaking_token(
    tmp_path: Path,
) -> None:
    args_file = tmp_path / "agent-args"
    agent_binary = _executable(
        tmp_path / AGENT_NAME,
        '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$AGENT_ARGS_FILE"\n',
    )
    env = _linux_environment(tmp_path)
    env["AGENT_ARGS_FILE"] = str(args_file)

    completed = _run_installer(
        tmp_path,
        [
            "--gateway",
            EXAMPLE_URL,
            "--join-token",
            "test-join-token",
            "--agent-bin",
            str(agent_binary),
            "--foreground",
            "--executor",
            "external",
        ],
        env=env,
    )

    assert completed.returncode == 0, completed.stderr
    args = args_file.read_text(encoding="utf-8").splitlines()
    assert args[args.index("--gateway") + 1] == EXAMPLE_URL
    assert args[args.index("--join-token") + 1] == "test-join-token"
    assert "test-join-token" not in completed.stdout
    assert "test-join-token" not in completed.stderr


def test_install_script_forwards_provider_identity_without_join_token(tmp_path: Path) -> None:
    args_file = tmp_path / "agent-args"
    agent_binary = _executable(
        tmp_path / AGENT_NAME,
        '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$AGENT_ARGS_FILE"\n',
    )
    env = _linux_environment(tmp_path)
    env["AGENT_ARGS_FILE"] = str(args_file)
    fake_bin = Path(env["PATH"].split(os.pathsep)[0])
    _executable(
        fake_bin / "docker",
        '#!/bin/sh\n[ "${1:-}" = "info" ] && exit 0\nexit 0\n',
    )
    enrollment_request = "123e4567-e89b-42d3-a456-426614174000"
    worker_image = f"registry.example.com/worker@sha256:{'b' * 64}"
    state_dir = tmp_path / "agent-state"

    completed = _run_installer(
        tmp_path,
        [
            "--gateway",
            EXAMPLE_URL,
            "--provider-enrollment-request",
            enrollment_request,
            "--provider",
            "aws",
            "--provider-instance-identity",
            "imds-v2",
            "--machine-fingerprint",
            "i-0123456789abcdef0",
            "--hostname",
            "i-0123456789abcdef0",
            "--agent-version",
            "0.1.0-acceptance.1",
            "--agent-sha256",
            "a" * 64,
            "--worker-image",
            worker_image,
            "--state-dir",
            str(state_dir),
            "--executor",
            "container",
            "--max-gpus",
            "0",
            "--install-docker",
            "auto",
            "--install-wireguard",
            "auto",
            "--background",
            "--agent-bin",
            str(agent_binary),
            "--foreground",
        ],
        env=env,
    )

    assert completed.returncode == 0, completed.stderr
    args = args_file.read_text(encoding="utf-8").splitlines()
    assert "--join-token" not in args
    assert args[args.index("--provider-enrollment-request") + 1] == enrollment_request
    assert args[args.index("--provider") + 1] == "aws"
    assert args[args.index("--provider-instance-identity") + 1] == "imds-v2"
    assert args[args.index("--machine-fingerprint") + 1] == "i-0123456789abcdef0"
    assert args[args.index("--worker-image") + 1] == worker_image


def test_install_script_verifies_pinned_versioned_agent_before_replacement(tmp_path: Path) -> None:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _fake_uname(fake_bin)
    source_agent = _executable(
        tmp_path / "source-agent",
        '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$AGENT_ARGS_FILE"\n',
    )
    _executable(
        fake_bin / "curl",
        """#!/bin/sh
set -eu
url=""
out=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) shift; out="$1" ;;
    http*) url="$1" ;;
  esac
  shift
done
cp "$SOURCE_AGENT" "$out"
printf '%s\n' "$url" > "$DOWNLOAD_URL_FILE"
""",
    )
    args_file = tmp_path / "agent-args"
    download_url_file = tmp_path / "download-url"
    home = tmp_path / "home"
    home.mkdir()
    installed_agent = home / ".lazycloud" / "bin" / AGENT_NAME
    installed_agent.parent.mkdir(parents=True)
    installed_agent.write_bytes(b"previous agent")
    digest = hashlib.sha256(source_agent.read_bytes()).hexdigest()
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "HOME": str(home),
            "SOURCE_AGENT": str(source_agent),
            "DOWNLOAD_URL_FILE": str(download_url_file),
            "AGENT_ARGS_FILE": str(args_file),
        }
    )

    completed = _run_installer(
        tmp_path,
        [
            "--gateway",
            EXAMPLE_URL,
            "--join-token",
            "test-join-token",
            "--agent-version",
            "2026.07.14",
            "--agent-sha256",
            digest,
            "--foreground",
            "--executor",
            "external",
        ],
        env=env,
    )

    assert completed.returncode == 0, completed.stderr
    assert (
        download_url_file.read_text(encoding="utf-8")
        .strip()
        .endswith("/install/agent/2026.07.14/linux/amd64")
    )
    assert args_file.read_text(encoding="utf-8").splitlines()[0] == "join"
    assert installed_agent.read_bytes() == source_agent.read_bytes()
    assert installed_agent.stat().st_mode & 0o111


def test_install_script_rejects_agent_artifact_digest_mismatch(tmp_path: Path) -> None:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _fake_uname(fake_bin)
    source_agent = _executable(tmp_path / "source-agent", "#!/bin/sh\nexit 0\n")
    _executable(
        fake_bin / "curl",
        """#!/bin/sh
set -eu
out=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-o" ]; then shift; out="$1"; fi
  shift
done
cp "$SOURCE_AGENT" "$out"
""",
    )
    home = tmp_path / "home"
    home.mkdir()
    installed_agent = home / ".lazycloud" / "bin" / AGENT_NAME
    installed_agent.parent.mkdir(parents=True)
    installed_agent.write_bytes(b"previous agent")
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "HOME": str(home),
            "SOURCE_AGENT": str(source_agent),
        }
    )

    completed = _run_installer(
        tmp_path,
        [
            "--gateway",
            EXAMPLE_URL,
            "--join-token",
            "test-join-token",
            "--agent-version",
            "2026.07.14",
            "--agent-sha256",
            "0" * 64,
            "--foreground",
            "--executor",
            "external",
        ],
        env=env,
    )

    assert completed.returncode == 1
    assert "SHA-256 mismatch" in completed.stderr
    assert installed_agent.read_bytes() == b"previous agent"
    assert not list(installed_agent.parent.glob(".agent.*"))


def test_install_script_refuses_missing_docker_when_install_is_disabled(tmp_path: Path) -> None:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _fake_uname(fake_bin)
    agent_binary = _executable(tmp_path / AGENT_NAME, "#!/bin/sh\nexit 0\n")
    env = _hermetic_environment(fake_bin, "sh", "sed", "tr")

    completed = _run_installer(
        tmp_path,
        [
            "--gateway",
            EXAMPLE_URL,
            "--join-token",
            "test-join-token",
            "--agent-bin",
            str(agent_binary),
            "--foreground",
            "--no-install-docker",
        ],
        env=env,
    )

    assert completed.returncode == 1
    assert "Docker is required for container execution" in completed.stderr
    assert "test-join-token" not in completed.stderr


def test_runtime_only_installs_no_agent_or_service(tmp_path: Path) -> None:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _fake_uname(fake_bin)
    for name in ("wg", "ip", "iptables"):
        _executable(fake_bin / name, "#!/bin/sh\nexit 0\n")
    _executable(
        fake_bin / "docker",
        '#!/bin/sh\n[ "${1:-}" = "info" ] && exit 0\nexit 0\n',
    )
    env = _hermetic_environment(fake_bin, "sh", "sed", "tr")

    completed = _run_installer(
        tmp_path,
        ["--runtime-only", "--foreground", "--executor", "container"],
        env=env,
    )

    assert completed.returncode == 0, completed.stderr
    assert not (Path(env["HOME"]) / ".lazycloud").exists()


def test_install_script_fails_before_changes_when_background_is_not_root(tmp_path: Path) -> None:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _fake_uname(fake_bin)
    _executable(fake_bin / "id", "#!/bin/sh\nprintf '1000\\n'\n")
    agent_binary = _executable(tmp_path / AGENT_NAME, "#!/bin/sh\nexit 0\n")
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:/usr/bin:/bin"

    completed = _run_installer(
        tmp_path,
        [
            "--gateway",
            EXAMPLE_URL,
            "--join-token",
            "test-join-token",
            "--agent-bin",
            str(agent_binary),
            "--background",
            "--executor",
            "external",
        ],
        env=env,
    )

    assert completed.returncode == 1
    assert "background installation requires root" in completed.stderr
    assert "test-join-token" not in completed.stderr


def test_background_installer_waits_for_runtime_ready_marker_and_active_service(
    tmp_path: Path,
) -> None:
    env = _linux_environment(tmp_path)
    fake_bin = Path(env["PATH"].split(os.pathsep)[0])
    state_dir = tmp_path / "agent-state"
    _executable(fake_bin / "id", "#!/bin/sh\nprintf '0\\n'\n")
    _executable(
        fake_bin / "docker",
        '#!/bin/sh\n[ "${1:-}" = "info" ] && exit 0\nexit 0\n',
    )
    _executable(
        fake_bin / "systemctl",
        """#!/bin/sh
set -eu
case "${1:-}" in
  show-environment) exit 0 ;;
  is-active) exit 0 ;;
  *) exit 0 ;;
esac
""",
    )
    agent_script = """#!/bin/sh
set -eu
state_dir=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--state-dir" ]; then shift; state_dir="$1"; fi
  shift
done
mkdir -p "$state_dir"
printf '{}\n' > "$state_dir/__READY_FILE__"
""".replace("__READY_FILE__", AGENT_RUNTIME_READY_FILE)
    agent_binary = _executable(
        tmp_path / AGENT_NAME,
        agent_script,
    )

    completed = _run_installer(
        tmp_path,
        [
            "--gateway",
            EXAMPLE_URL,
            "--join-token",
            "test-join-token",
            "--agent-bin",
            str(agent_binary),
            "--background",
            "--state-dir",
            str(state_dir),
            "--executor",
            "external",
        ],
        env=env,
    )

    assert completed.returncode == 0, completed.stderr
    assert (state_dir / AGENT_RUNTIME_READY_FILE).is_file()
    assert "test-join-token" not in completed.stdout
    assert "test-join-token" not in completed.stderr


def test_background_installer_fails_with_redacted_service_diagnostics(tmp_path: Path) -> None:
    env = _linux_environment(tmp_path)
    env["LAZYCLOUD_AGENT_READY_TIMEOUT_SECONDS"] = "1"
    fake_bin = Path(env["PATH"].split(os.pathsep)[0])
    _executable(fake_bin / "id", "#!/bin/sh\nprintf '0\\n'\n")
    _executable(fake_bin / "sleep", "#!/bin/sh\nexit 0\n")
    _executable(
        fake_bin / "docker",
        '#!/bin/sh\n[ "${1:-}" = "info" ] && exit 0\nexit 0\n',
    )
    _executable(
        fake_bin / "systemctl",
        """#!/bin/sh
case "${1:-}" in
  show-environment) exit 0 ;;
  is-active) exit 1 ;;
  show) printf 'ActiveState=failed\nSubState=failed\nResult=exit-code\nNRestarts=5\n'; exit 0 ;;
  *) exit 0 ;;
esac
""",
    )
    _executable(
        fake_bin / "journalctl",
        """#!/bin/sh
printf '%s\n' \
  'error: https://sts.example.com/?X-Amz-Signature=signed-secret' \
  'Authorization: Bearer bearer-secret' \
  'token=transport-secret'
""",
    )
    agent_binary = _executable(tmp_path / AGENT_NAME, "#!/bin/sh\nexit 0\n")

    completed = _run_installer(
        tmp_path,
        [
            "--gateway",
            EXAMPLE_URL,
            "--join-token",
            "test-join-token",
            "--agent-bin",
            str(agent_binary),
            "--background",
            "--state-dir",
            str(tmp_path / "agent-state"),
            "--executor",
            "external",
        ],
        env=env,
    )

    assert completed.returncode == 1
    assert "ActiveState=failed" in completed.stderr
    assert "[REDACTED]" in completed.stderr
    assert "signed-secret" not in completed.stderr
    assert "bearer-secret" not in completed.stderr
    assert "transport-secret" not in completed.stderr
    assert "test-join-token" not in completed.stdout
    assert "test-join-token" not in completed.stderr


def _run_installer(
    tmp_path: Path,
    args: list[str],
    *,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    script = tmp_path / "install-agent.sh"
    script.write_text(build_agent_install_script(), encoding="utf-8")
    script.chmod(0o755)
    return subprocess.run(
        ["sh", str(script), *args],
        text=True,
        capture_output=True,
        env=env,
        check=False,
        timeout=10,
    )


def _linux_environment(tmp_path: Path) -> dict[str, str]:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir(exist_ok=True)
    _fake_uname(fake_bin)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{os.environ.get('PATH', '')}"
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env["HOME"] = str(home)
    return env


def _hermetic_environment(fake_bin: Path, *host_utilities: str) -> dict[str, str]:
    """Limit dependency probes to the selected commands."""
    for utility in host_utilities:
        resolved = shutil.which(utility)
        assert resolved is not None, f"the installer requires host utility {utility}"
        (fake_bin / utility).symlink_to(resolved)
    env = os.environ.copy()
    env["PATH"] = str(fake_bin)
    home = fake_bin.parent / "home"
    home.mkdir(exist_ok=True)
    env["HOME"] = str(home)
    return env


def _fake_uname(fake_bin: Path) -> None:
    _executable(fake_bin / "id", "#!/bin/sh\nprintf '1000\\n'\n")
    _executable(
        fake_bin / "uname",
        """#!/bin/sh
if [ "${1:-}" = "-s" ]; then printf 'Linux\\n'; else printf 'x86_64\\n'; fi
""",
    )
    _executable(fake_bin / "wg", "#!/bin/sh\nexit 0\n")
    _executable(fake_bin / "ip", "#!/bin/sh\nexit 0\n")
    _executable(fake_bin / "iptables", "#!/bin/sh\nexit 0\n")


def _executable(path: Path, contents: str) -> Path:
    # Never write through a symlink into a real host binary.
    path.unlink(missing_ok=True)
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o755)
    return path
