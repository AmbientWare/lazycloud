from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from agent.operations import redact_telemetry
from cli.agent_install import (
    AgentInstallCommandResult,
    AgentInstallEnvironment,
    AgentInstallRequest,
    AgentInstallState,
    AgentServiceManager,
    AgentServiceScope,
    install_agent_service,
    plan_agent_service_install,
)
from cli.main import build_admin_cli
from fastapi.testclient import TestClient
from pydantic import JsonValue, TypeAdapter
from shared.app_identity import AGENT_NAME
from shared.compute_policy import MachinePool
from shared.http_transport import HttpChannel
from tests.url_constants import EXAMPLE_COM_URL

cli = build_admin_cli()

_JSON_VALUE_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


class _TestClientHttpChannel(HttpChannel):
    def __init__(self, client: TestClient, *, token: str) -> None:
        super().__init__(token=token)
        self._client = client

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, JsonValue] | None = None,
    ) -> JsonValue:
        headers = {"Authorization": f"Bearer {self.token}"}
        response = self._client.request(
            method,
            path,
            headers=headers,
            json=dict(payload) if payload is not None else None,
        )
        assert response.status_code < 400, response.text
        if response.status_code == 204:
            return None
        return _JSON_VALUE_ADAPTER.validate_python(response.json())


@dataclass(slots=True)
class _FakeInstallRunner:
    available: set[str] = field(default_factory=lambda: {"systemctl", "launchctl"})
    commands: list[list[str]] = field(default_factory=list)

    def which(self, name: str) -> str | None:
        return f"/usr/bin/{name}" if name in self.available else None

    def run(self, command: list[str], *, ignore_failure: bool = False) -> AgentInstallCommandResult:
        self.commands.append(command)
        return AgentInstallCommandResult(command=command, ignored=ignore_failure)


def test_agent_telemetry_never_reports_secret_material() -> None:
    line = (
        "Authorization: Bearer secret-token "
        "AWS_SECRET_ACCESS_KEY=aws-value apiKey=api-value password=pass"
    )

    redacted = redact_telemetry(line)

    assert "secret-token" not in redacted
    assert "aws-value" not in redacted
    assert "api-value" not in redacted
    assert "password=pass" not in redacted


def test_agent_install_writes_token_config_service_and_runs_commands(tmp_path: Path) -> None:
    runner = _FakeInstallRunner()
    environment = AgentInstallEnvironment(
        os_name="linux",
        uid=0,
        home=tmp_path / "root",
        systemd_unit_dir=tmp_path / "systemd",
    )
    result = install_agent_service(
        AgentInstallRequest(
            name="worker-a",
            pool=MachinePool("gpu"),
            endpoint=EXAMPLE_COM_URL,
            join_token="secret-token-value",
            version="v1",
            labels={"zone": "us"},
            state_dir=str(tmp_path / "state"),
        ),
        environment=environment,
        runner=runner,
    )

    assert result.state is AgentInstallState.Installed
    assert Path(result.token_path).read_text(encoding="utf-8") == "secret-token-value\n"
    assert Path(result.token_path).stat().st_mode & 0o777 == 0o600
    config = Path(result.config_path).read_text(encoding="utf-8")
    assert '"pool": "gpu"' in config
    assert '"token_file":' in config
    assert "secret-token-value" not in config
    assert result.service is not None
    service_path = Path(result.service.target_path)
    assert service_path.exists()
    assert f"{AGENT_NAME}-worker-a.service" in service_path.name
    assert runner.commands == result.service.commands


def test_agent_install_plan_keeps_the_join_token_out_of_the_service_unit(
    tmp_path: Path,
) -> None:
    launchd = plan_agent_service_install(
        AgentInstallRequest(
            name="private pool",
            endpoint=EXAMPLE_COM_URL,
            join_token="private-pool-join-secret",
            state_dir=str(tmp_path / "state"),
        ),
        environment=AgentInstallEnvironment(os_name="darwin", uid=501, home=tmp_path / "home"),
    )
    assert launchd.manager is AgentServiceManager.Launchd
    assert launchd.scope is AgentServiceScope.User
    assert launchd.service is not None
    assert "private-pool-join-secret" not in launchd.service.content
    assert launchd.token_path in launchd.service.content

    unsupported = plan_agent_service_install(
        AgentInstallRequest(name="worker-a"),
        environment=AgentInstallEnvironment(os_name="freebsd", uid=1000, home=tmp_path),
    )
    assert unsupported.state is AgentInstallState.Unsupported
    assert unsupported.supported is False
    assert unsupported.service is None
