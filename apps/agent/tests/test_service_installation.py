from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from agent.operations import (
    AGENT_SOURCE_CACHE_RELATIVE_PATH,
    AgentBootstrap,
    AgentHostStatus,
    AgentState,
    AgentWorkerSlot,
)
from agent.service_manager import (
    AgentRemoteLeaveResult,
    AgentServiceInstallResult,
    AgentServiceOperationResult,
    AgentServiceRuntimeStatus,
    ServiceCommand,
    ServiceCommandResult,
    ServicePlatform,
)
from agent_app import main as agent_main
from gateway.http import LeaveAgentRequest, LeaveAgentResponse
from shared.app_identity import AGENT_NAME
from shared.compute_policy import MachinePool
from shared.http.errors import ErrorResponse
from shared.usage import UsageBillingOwner
from tests.http_server import running_http_server
from tests.url_constants import EXAMPLE_URL
from worker.source_cache_cleanup import (
    WorkerSourceCacheDestructionReceipt,
    WorkerSourceCacheIdentity,
    record_source_cache_session,
)

TEST_AGENT_CREDENTIAL_ID = "11111111-1111-4111-8111-111111111111"
TEST_AGENT_CREDENTIAL_GENERATION = 1


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
    assert payload.dry_run
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


@dataclass(slots=True)
class _Installation:
    state_dir: Path
    unit_path: Path
    sibling_state: Path
    sibling_unit: Path
    state: AgentState
    status: int = 200
    requests: list[LeaveAgentRequest] = field(default_factory=list)
    local_authority_present: list[bool] = field(default_factory=list)

    @property
    def state_path(self) -> Path:
        return self.state_dir / "agent-state.json"


@pytest.fixture
def installation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Installation]:
    from agent import service_manager

    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    state_dir = tmp_path / "state" / "agent"
    state_dir.mkdir(parents=True)
    sibling_state = tmp_path / "state" / "other-agent"
    sibling_state.mkdir()
    (sibling_state / "keep").write_bytes(b"other owner")
    unit_path = unit_dir / f"{AGENT_NAME}.service"
    unit_path.write_text("[Service]\n", encoding="utf-8")
    sibling_unit = unit_dir / "other-agent.service"
    sibling_unit.write_bytes(b"other service")
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
    installed = _Installation(state_dir, unit_path, sibling_state, sibling_unit, state)

    class GatewayHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            payload = LeaveAgentRequest.model_validate_json(
                self.rfile.read(int(self.headers["Content-Length"]))
            )
            installed.requests.append(payload)
            installed.local_authority_present.append(
                installed.state_path.exists() and installed.unit_path.exists()
            )
            if self.path != "/gateway/agents/leave":
                self.send_error(404)
                return
            if installed.status == 200:
                response = LeaveAgentResponse(machine_id=state.machine_id).model_dump_json()
            else:
                response = ErrorResponse(
                    detail="invalid agent token"
                    if installed.status == 400
                    else "gateway unavailable"
                ).model_dump_json()
            body = response.encode()
            self.send_response(installed.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    def allow_manager(platform: ServicePlatform, *, mutation: bool) -> None:
        pass

    def run_commands(commands: list[ServiceCommand]) -> list[ServiceCommandResult]:
        return [ServiceCommandResult(argv=command.argv, returncode=0) for command in commands]

    def service_status(
        *, platform: ServicePlatform, service_name: str
    ) -> AgentServiceRuntimeStatus:
        return AgentServiceRuntimeStatus(
            platform=platform,
            service_name=service_name,
            target_path=str(unit_path),
            installed=unit_path.exists(),
            active=False,
            enabled=False,
        )

    monkeypatch.setattr(service_manager, "DEFAULT_SYSTEMD_UNIT_DIR", str(unit_dir))
    monkeypatch.setattr(agent_main, "_require_service_manager", allow_manager)
    monkeypatch.setattr(agent_main, "_run_service_commands", run_commands)
    monkeypatch.setattr(agent_main, "_service_status", service_status)
    server = ThreadingHTTPServer(("127.0.0.1", 0), GatewayHandler)
    state.gateway_url = f"http://127.0.0.1:{server.server_port}"
    installed.state_path.write_text(state.model_dump_json(), encoding="utf-8")
    with running_http_server(server):
        yield installed


def test_leave_retries_remote_decommission_before_removing_local_authority(
    installation: _Installation, capsys: pytest.CaptureFixture[str]
) -> None:
    cache_root = installation.state_dir / AGENT_SOURCE_CACHE_RELATIVE_PATH
    identity = WorkerSourceCacheIdentity.open(cache_root, storage_id="machine:machine-one")
    record_source_cache_session(cache_root, identity, session_fence=9)
    (cache_root / "source.py").write_bytes(b"sensitive source")
    installation.status = 503
    args = ["leave", "--target", "systemd", "--state-dir", str(installation.state_dir)]

    with pytest.raises(SystemExit) as failure:
        agent_main.main(args)

    assert failure.value.code == 1
    refused = capsys.readouterr()
    assert "gateway unavailable" in refused.err
    assert "persisted-agent-secret" not in refused.out + refused.err
    assert installation.state_path.exists()
    assert installation.unit_path.exists()
    assert not cache_root.exists()
    receipt_path = installation.state_dir / agent_main.SOURCE_CACHE_DESTRUCTION_RECEIPT_FILE
    receipt = WorkerSourceCacheDestructionReceipt.model_validate_json(receipt_path.read_text())
    assert receipt.session_fence == 9
    assert receipt_path.stat().st_mode & 0o777 == 0o600

    installation.status = 200
    agent_main.main(args)

    output = capsys.readouterr().out
    result = AgentServiceOperationResult.model_validate_json(output)
    assert result.remote_leave == AgentRemoteLeaveResult(machine_id="machine-one")
    assert result.service_removed and result.state_removed
    assert "persisted-agent-secret" not in output
    assert len(installation.requests) == 2
    first, retry = installation.requests
    assert first == retry
    assert retry.agent_token == installation.state.agent_token
    assert retry.machine_id == installation.state.machine_id
    assert retry.cache_generation_id == identity.generation_id
    assert retry.cache_session_fence == 9
    assert all(installation.local_authority_present)
    assert not installation.state_dir.exists()
    assert not installation.unit_path.exists()
    assert (installation.sibling_state / "keep").read_bytes() == b"other owner"
    assert installation.sibling_unit.read_bytes() == b"other service"


def test_leave_treats_revoked_remote_authority_as_already_absent(
    installation: _Installation, capsys: pytest.CaptureFixture[str]
) -> None:
    installation.status = 400

    agent_main.main(["leave", "--target", "systemd", "--state-dir", str(installation.state_dir)])

    result = AgentServiceOperationResult.model_validate_json(capsys.readouterr().out)
    assert result.remote_leave is not None and result.remote_leave.already_absent
    assert result.remote_leave.machine_id == installation.state.machine_id
    assert not installation.state_dir.exists()
    assert not installation.unit_path.exists()


def test_leave_refuses_local_cleanup_without_saved_identity(
    installation: _Installation, capsys: pytest.CaptureFixture[str]
) -> None:
    installation.state_path.unlink()

    with pytest.raises(SystemExit) as failure:
        agent_main.main(
            ["leave", "--target", "systemd", "--state-dir", str(installation.state_dir)]
        )

    assert failure.value.code == 1
    assert "saved agent identity is missing" in capsys.readouterr().err
    assert not installation.requests
    assert installation.unit_path.exists()
    assert installation.state_dir.exists()
