from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from agent.operations import (
    AGENT_RUNTIME_READY_FILE,
    AgentBootstrap,
    AgentHostStatus,
    AgentState,
    AgentWorkerSlot,
    build_agent_install_script,
)
from agent.service_manager import (
    AgentRemoteLeaveResult,
    AgentServiceInstallResult,
    AgentServiceOperationResult,
    AgentServiceRuntimeStatus,
    ServiceCommand,
    ServiceCommandResult,
    ServiceLifecycleAction,
    ServicePlatform,
)
from agent_app import main as agent_main
from gateway.http import LeaveAgentRequest, LeaveAgentResponse
from shared.app_identity import AGENT_NAME
from shared.compute_policy import MachinePool
from shared.http.errors import ErrorResponse, HttpApiError
from shared.newt_install import NEWT_AMD64_SHA256, NEWT_INSTALL_VERSION
from shared.usage import UsageBillingOwner
from tests.url_constants import EXAMPLE_URL
from worker.source_cache_cleanup import (
    WorkerSourceCacheDestructionReceipt,
    WorkerSourceCacheIdentity,
    record_source_cache_session,
)

TEST_AGENT_CREDENTIAL_ID = "11111111-1111-4111-8111-111111111111"
TEST_AGENT_CREDENTIAL_GENERATION = 1


class _LeaveClient:
    def __init__(
        self,
        *,
        response: LeaveAgentResponse | None = None,
        error: HttpApiError | None = None,
    ) -> None:
        self.response = response or LeaveAgentResponse(machine_id="machine-one")
        self.error = error
        self.requests: list[LeaveAgentRequest] = []

    def leave_agent(self, request: LeaveAgentRequest) -> LeaveAgentResponse:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.response


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
    assert args_file.read_text(encoding="utf-8").splitlines()[:4] == [
        "join",
        "--gateway",
        EXAMPLE_URL,
        "--join-token",
    ]
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
            "--install-newt",
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
    assert args == [
        "join",
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
        "--executor",
        "container",
        "--worker-image",
        worker_image,
        "--max-gpus",
        "0",
        "--state-dir",
        str(state_dir),
    ]
    assert not any(argument.startswith("--cloud-") for argument in args)


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
    assert not (home / ".lazycloud" / "bin" / AGENT_NAME).exists()


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


def test_install_script_installs_pinned_newt_binary(tmp_path: Path) -> None:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _fake_uname(fake_bin)
    (fake_bin / "newt").unlink()
    _executable(fake_bin / "id", "#!/bin/sh\nprintf '0\\n'\n")
    _executable(
        fake_bin / "curl",
        (
            """#!/bin/sh
set -eu
out=""
url=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) shift; out="$1" ;;
    http*) url="$1" ;;
  esac
  shift
done
printf '#!/bin/sh\nprintf "Newt version __NEWT_VERSION__\\n"\n' > "$out"
chmod 0755 "$out"
printf '%s\n' "$url" > "$NEWT_URL_FILE"
"""
        ).replace("__NEWT_VERSION__", NEWT_INSTALL_VERSION),
    )
    _executable(
        fake_bin / "sha256sum",
        f"#!/bin/sh\nprintf '{NEWT_AMD64_SHA256}  %s\\n' \"$1\"\n",
    )
    _executable(
        fake_bin / "install",
        """#!/bin/sh
set -eu
source="$3"
destination="$4"
target="$FAKE_BIN/$(basename "$destination")"
cp "$source" "$target"
chmod 0755 "$target"
""",
    )
    args_file = tmp_path / "agent-args"
    newt_url_file = tmp_path / "newt-url"
    agent_binary = _executable(
        tmp_path / AGENT_NAME,
        '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$AGENT_ARGS_FILE"\n',
    )
    env = _hermetic_environment(
        fake_bin,
        "sh",
        "sed",
        "tr",
        "awk",
        "basename",
        "chmod",
        "cp",
        "mkdir",
        "mktemp",
        "rm",
    )
    env.update(
        {
            "FAKE_BIN": str(fake_bin),
            "AGENT_ARGS_FILE": str(args_file),
            "NEWT_URL_FILE": str(newt_url_file),
        }
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
            "--foreground",
            "--executor",
            "external",
        ],
        env=env,
    )

    assert completed.returncode == 0, completed.stderr
    assert f"Installing Newt {NEWT_INSTALL_VERSION}" in completed.stderr
    assert (
        newt_url_file.read_text(encoding="utf-8")
        .strip()
        .endswith(f"/{NEWT_INSTALL_VERSION}/newt_linux_amd64")
    )
    assert (fake_bin / "newt").is_file()


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
    assert f"Waiting for {AGENT_NAME} enrollment" in completed.stderr
    assert f"{AGENT_NAME} enrolled and ready" in completed.stderr
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


def test_service_dry_run_never_serializes_raw_join_token(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    join_token = "one-time-bootstrap-secret"

    agent_main.main(
        [
            "install-service",
            "--gateway",
            EXAMPLE_URL,
            "--join-token",
            join_token,
            "--state-dir",
            str(tmp_path),
            "--target",
            "systemd",
            "--dry-run",
        ]
    )

    output = capsys.readouterr().out
    payload = AgentServiceInstallResult.model_validate_json(output)
    assert join_token not in output
    assert payload.command[payload.command.index("--join-token-file") + 1] == str(
        tmp_path / "join-token"
    )
    assert not (tmp_path / "join-token").exists()


def test_status_validates_state_and_never_outputs_agent_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = AgentState(
        gateway_url=EXAMPLE_URL,
        workspace_id="workspace-one",
        pool=MachinePool("customer-cpu"),
        machine_id="machine-one",
        agent_token="persisted-agent-secret",
        credential_id=TEST_AGENT_CREDENTIAL_ID,
        credential_generation=TEST_AGENT_CREDENTIAL_GENERATION,
        bootstrap=AgentBootstrap(gateway_public_http_url=EXAMPLE_URL),
    )
    (tmp_path / "agent-state.json").write_text(state.model_dump_json(), encoding="utf-8")
    slots = [
        AgentWorkerSlot(
            worker_id="worker-one",
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            billing_owner=UsageBillingOwner.SelfHosted,
        )
    ]
    (tmp_path / "active-worker-slots.json").write_text(
        json.dumps([slot.model_dump(mode="json") for slot in slots]),
        encoding="utf-8",
    )

    def service_status(
        *,
        platform: ServicePlatform,
        service_name: str,
    ) -> AgentServiceRuntimeStatus:
        _ = platform, service_name
        return AgentServiceRuntimeStatus(
            platform=ServicePlatform.Systemd,
            service_name=AGENT_NAME,
            target_path=f"/etc/systemd/system/{AGENT_NAME}.service",
            installed=True,
            active=True,
            enabled=True,
        )

    monkeypatch.setattr(agent_main, "_service_status", service_status)

    agent_main.main(["status", "--state-dir", str(tmp_path), "--target", "systemd"])

    output = capsys.readouterr().out
    payload = AgentHostStatus.model_validate_json(output)
    assert "persisted-agent-secret" not in output
    assert payload.joined is True
    assert payload.active_worker_count == 1
    assert payload.service.active is True


def test_leave_removes_service_and_private_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from agent import service_manager

    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    state_dir = tmp_path / "state" / "agent"
    state_dir.mkdir(parents=True)
    state = AgentState(
        gateway_url=EXAMPLE_URL,
        workspace_id="workspace-one",
        pool=MachinePool("customer-cpu"),
        machine_id="machine-one",
        agent_token="persisted-agent-secret",
        credential_id=TEST_AGENT_CREDENTIAL_ID,
        credential_generation=TEST_AGENT_CREDENTIAL_GENERATION,
        bootstrap=AgentBootstrap(gateway_public_http_url=EXAMPLE_URL),
    )
    state_path = state_dir / "agent-state.json"
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    unit_path = unit_dir / f"{AGENT_NAME}.service"
    unit_path.write_text("[Service]\n", encoding="utf-8")
    events: list[str] = []
    monkeypatch.setattr(service_manager, "DEFAULT_SYSTEMD_UNIT_DIR", str(unit_dir))
    monkeypatch.setattr(agent_main, "_require_service_manager", _allow_service_manager)

    def run_commands(commands: list[ServiceCommand]) -> list[ServiceCommandResult]:
        events.append("stop" if not events else "post-cleanup")
        return [ServiceCommandResult(argv=command.argv, returncode=0) for command in commands]

    def service_status(
        *,
        platform: ServicePlatform,
        service_name: str,
    ) -> AgentServiceRuntimeStatus:
        _ = platform, service_name
        events.append("verified-stopped")
        return AgentServiceRuntimeStatus(
            platform=ServicePlatform.Systemd,
            service_name=AGENT_NAME,
            target_path=str(unit_path),
            installed=True,
            active=False,
            enabled=False,
        )

    def leave_remote(
        saved: AgentState,
        *,
        cache_destruction: WorkerSourceCacheDestructionReceipt | None,
    ) -> AgentRemoteLeaveResult:
        assert saved.agent_token == "persisted-agent-secret"
        assert cache_destruction is None
        assert state_path.exists()
        assert unit_path.exists()
        events.append("remote-leave")
        return AgentRemoteLeaveResult(machine_id=saved.machine_id)

    monkeypatch.setattr(agent_main, "_run_service_commands", run_commands)
    monkeypatch.setattr(agent_main, "_service_status", service_status)
    monkeypatch.setattr(agent_main, "_leave_remote_agent", leave_remote)

    agent_main.main(
        [
            "leave",
            "--target",
            "systemd",
            "--state-dir",
            str(state_dir),
        ]
    )

    output = capsys.readouterr().out
    payload = AgentServiceOperationResult.model_validate_json(output)
    assert "persisted-agent-secret" not in output
    assert payload.action is ServiceLifecycleAction.Leave
    assert payload.remote_leave == AgentRemoteLeaveResult(machine_id="machine-one")
    assert payload.service_removed is True
    assert payload.state_removed is True
    assert events == ["stop", "verified-stopped", "remote-leave", "post-cleanup"]
    assert not unit_path.exists()
    assert not state_dir.exists()


def test_remote_leave_authenticates_with_saved_machine_credential() -> None:
    state = AgentState(
        gateway_url=EXAMPLE_URL,
        workspace_id="workspace-one",
        pool=MachinePool("customer-cpu"),
        machine_id="machine-one",
        agent_token="persisted-agent-secret",
        credential_id=TEST_AGENT_CREDENTIAL_ID,
        credential_generation=TEST_AGENT_CREDENTIAL_GENERATION,
        bootstrap=AgentBootstrap(gateway_public_http_url=EXAMPLE_URL),
    )
    client = _LeaveClient()

    result = agent_main._leave_remote_agent(state, client=client)

    assert result == AgentRemoteLeaveResult(machine_id="machine-one")
    assert client.requests == [
        LeaveAgentRequest(
            agent_token="persisted-agent-secret",
            machine_id="machine-one",
        )
    ]
    assert "persisted-agent-secret" not in result.model_dump_json()


def test_leave_cache_destruction_receipt_survives_gateway_retry(tmp_path: Path) -> None:
    state_dir = tmp_path / "state" / "agent"
    cache_root = state_dir / "cache"
    state = AgentState(
        gateway_url=EXAMPLE_URL,
        workspace_id="workspace-one",
        pool=MachinePool("customer-cpu"),
        machine_id="machine-one",
        agent_token="persisted-agent-secret",
        credential_id=TEST_AGENT_CREDENTIAL_ID,
        credential_generation=TEST_AGENT_CREDENTIAL_GENERATION,
        bootstrap=AgentBootstrap(gateway_public_http_url=EXAMPLE_URL),
    )
    identity = WorkerSourceCacheIdentity.open(
        cache_root,
        storage_id="machine:machine-one",
    )
    record_source_cache_session(cache_root, identity, session_fence=9)
    (cache_root / "source.py").write_text("sensitive", encoding="utf-8")

    first = agent_main._prepare_source_cache_destruction(state_dir, state)
    retry = agent_main._prepare_source_cache_destruction(state_dir, state)

    assert first == retry
    assert first is not None
    assert first.session_fence == 9
    assert not cache_root.exists()
    assert (state_dir / agent_main.SOURCE_CACHE_DESTRUCTION_RECEIPT_FILE).is_file()


def test_remote_leave_sends_exact_cache_destruction_session() -> None:
    state = AgentState(
        gateway_url=EXAMPLE_URL,
        workspace_id="workspace-one",
        pool=MachinePool("customer-cpu"),
        machine_id="machine-one",
        agent_token="persisted-agent-secret",
        credential_id=TEST_AGENT_CREDENTIAL_ID,
        credential_generation=TEST_AGENT_CREDENTIAL_GENERATION,
        bootstrap=AgentBootstrap(gateway_public_http_url=EXAMPLE_URL),
    )
    client = _LeaveClient()
    receipt = WorkerSourceCacheDestructionReceipt(
        generation_id="13f4ff1a-2562-47e7-9f08-964588090ee0",
        storage_id="machine:machine-one",
        session_fence=4,
    )

    agent_main._leave_remote_agent(state, client=client, cache_destruction=receipt)

    assert client.requests == [
        LeaveAgentRequest(
            agent_token="persisted-agent-secret",
            machine_id="machine-one",
            cache_generation_id=receipt.generation_id,
            cache_session_fence=4,
        )
    ]


def test_remote_leave_treats_revoked_credential_as_already_absent() -> None:
    state = AgentState(
        gateway_url=EXAMPLE_URL,
        workspace_id="workspace-one",
        pool=MachinePool("customer-cpu"),
        machine_id="machine-one",
        agent_token="revoked-agent-secret",
        credential_id=TEST_AGENT_CREDENTIAL_ID,
        credential_generation=TEST_AGENT_CREDENTIAL_GENERATION,
        bootstrap=AgentBootstrap(gateway_public_http_url=EXAMPLE_URL),
    )
    client = _LeaveClient(
        error=HttpApiError(
            "invalid agent token",
            status_code=400,
            error=ErrorResponse(detail="invalid agent token"),
        )
    )

    result = agent_main._leave_remote_agent(state, client=client)

    assert result.completed is True
    assert result.already_absent is True
    assert result.machine_id == "machine-one"


def test_remote_leave_surfaces_upstream_failure_and_preserves_local_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent import service_manager

    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    state_dir = tmp_path / "state" / "agent"
    state_dir.mkdir(parents=True)
    state = AgentState(
        gateway_url=EXAMPLE_URL,
        workspace_id="workspace-one",
        pool=MachinePool("customer-cpu"),
        machine_id="machine-one",
        agent_token="persisted-agent-secret",
        credential_id=TEST_AGENT_CREDENTIAL_ID,
        credential_generation=TEST_AGENT_CREDENTIAL_GENERATION,
        bootstrap=AgentBootstrap(gateway_public_http_url=EXAMPLE_URL),
    )
    state_path = state_dir / "agent-state.json"
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    unit_path = unit_dir / f"{AGENT_NAME}.service"
    unit_path.write_text("[Service]\n", encoding="utf-8")
    monkeypatch.setattr(service_manager, "DEFAULT_SYSTEMD_UNIT_DIR", str(unit_dir))
    monkeypatch.setattr(agent_main, "_require_service_manager", _allow_service_manager)

    def run_commands(commands: list[ServiceCommand]) -> list[ServiceCommandResult]:
        return [ServiceCommandResult(argv=command.argv, returncode=0) for command in commands]

    def service_status(
        *,
        platform: ServicePlatform,
        service_name: str,
    ) -> AgentServiceRuntimeStatus:
        _ = platform, service_name
        return AgentServiceRuntimeStatus(
            platform=ServicePlatform.Systemd,
            service_name=AGENT_NAME,
            target_path=str(unit_path),
            installed=True,
            active=False,
            enabled=False,
        )

    monkeypatch.setattr(agent_main, "_run_service_commands", run_commands)
    monkeypatch.setattr(agent_main, "_service_status", service_status)
    failure = HttpApiError("gateway unavailable", status_code=503)

    def fail_remote_leave(
        _state: AgentState,
        *,
        cache_destruction: WorkerSourceCacheDestructionReceipt | None,
    ) -> AgentRemoteLeaveResult:
        assert cache_destruction is None
        raise failure

    monkeypatch.setattr(agent_main, "_leave_remote_agent", fail_remote_leave)
    args = agent_main.build_parser().parse_args(
        ["leave", "--target", "systemd", "--state-dir", str(state_dir)],
        namespace=agent_main.AgentCommandArgs(),
    )

    with pytest.raises(HttpApiError, match="gateway unavailable"):
        agent_main._manage_service(args)

    assert state_path.exists()
    assert unit_path.exists()


def test_leave_refuses_local_cleanup_when_saved_identity_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent import service_manager

    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    state_dir = tmp_path / "state" / "agent"
    state_dir.mkdir(parents=True)
    unit_path = unit_dir / f"{AGENT_NAME}.service"
    unit_path.write_text("[Service]\n", encoding="utf-8")
    commands_run = False
    monkeypatch.setattr(service_manager, "DEFAULT_SYSTEMD_UNIT_DIR", str(unit_dir))
    monkeypatch.setattr(agent_main, "_require_service_manager", _allow_service_manager)

    def run_commands(_commands: list[ServiceCommand]) -> list[ServiceCommandResult]:
        nonlocal commands_run
        commands_run = True
        return []

    monkeypatch.setattr(agent_main, "_run_service_commands", run_commands)
    args = agent_main.build_parser().parse_args(
        ["leave", "--target", "systemd", "--state-dir", str(state_dir)],
        namespace=agent_main.AgentCommandArgs(),
    )

    with pytest.raises(RuntimeError, match="saved agent identity is missing"):
        agent_main._manage_service(args)

    assert commands_run is False
    assert unit_path.exists()
    assert state_dir.exists()


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
    )


def _allow_service_manager(
    platform: ServicePlatform,
    *,
    mutation: bool,
) -> None:
    _ = platform, mutation


def _linux_environment(tmp_path: Path) -> dict[str, str]:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir(exist_ok=True)
    _fake_uname(fake_bin)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{os.environ.get('PATH', '')}"
    return env


def _hermetic_environment(fake_bin: Path, *host_utilities: str) -> dict[str, str]:
    """Installer environment whose PATH resolves only fakes and the named host tools.

    The installer probes the host for Docker and Newt, so a PATH that still
    reaches /usr/bin makes the result depend on what the developer happens to
    have installed. Naming every real utility keeps the probe outcome under the
    test's control.
    """
    for utility in host_utilities:
        resolved = shutil.which(utility)
        assert resolved is not None, f"the installer requires host utility {utility}"
        (fake_bin / utility).symlink_to(resolved)
    env = os.environ.copy()
    env["PATH"] = str(fake_bin)
    return env


def _fake_uname(fake_bin: Path) -> None:
    _executable(
        fake_bin / "uname",
        """#!/bin/sh
if [ "${1:-}" = "-s" ]; then printf 'Linux\\n'; else printf 'x86_64\\n'; fi
""",
    )
    version_script = f"#!/bin/sh\nprintf 'Newt version {NEWT_INSTALL_VERSION}\\n'\n"
    _executable(fake_bin / "newt", version_script)


def _executable(path: Path, contents: str) -> Path:
    # Never write through a symlink into a real host binary.
    path.unlink(missing_ok=True)
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o755)
    return path
