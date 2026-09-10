from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from cli.agent_install import (
    AgentInstallCommandResult,
    AgentInstallEnvironment,
    AgentInstallRequest,
    AgentInstallState,
    install_agent_service,
)
from shared.compute_policy import MachinePool
from tests.url_constants import EXAMPLE_COM_URL


@dataclass(slots=True)
class _FakeInstallRunner:
    available: set[str] = field(default_factory=lambda: {"systemctl", "launchctl"})

    def which(self, name: str) -> str | None:
        return f"/usr/bin/{name}" if name in self.available else None

    def run(self, command: list[str], *, ignore_failure: bool = False) -> AgentInstallCommandResult:
        return AgentInstallCommandResult(command=command, ignored=ignore_failure)


@pytest.mark.parametrize(("os_name", "uid"), [("linux", 0), ("darwin", 501)])
def test_agent_install_keeps_credentials_in_a_private_file(
    tmp_path: Path, os_name: str, uid: int
) -> None:
    runner = _FakeInstallRunner()
    environment = AgentInstallEnvironment(
        os_name=os_name,
        uid=uid,
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
    assert json.loads(config)["token_file"] == result.token_path
    assert "secret-token-value" not in config
    assert result.service is not None
    service_path = Path(result.service.target_path)
    assert service_path.exists()
    service = service_path.read_text(encoding="utf-8")
    assert "secret-token-value" not in service
    assert result.token_path in service
