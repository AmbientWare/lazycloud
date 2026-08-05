from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from agent.service_manager import (
    DEFAULT_AGENT_STATE_DIR,
    DEFAULT_LAUNCHD_SYSTEM_DIR,
    DEFAULT_LAUNCHD_USER_DIR,
    DEFAULT_SYSTEMD_UNIT_DIR,
    AgentServiceSpec,
    RenderedServiceInstallPlan,
    ServicePlatform,
    launchd_label,
    normalize_service_name,
    render_launchd_plist,
    render_systemd_unit,
)
from pydantic import Field
from shared.app_identity import AGENT_NAME, AGENT_SERVICE_DESCRIPTION
from shared.compute_policy import MachinePool
from shared.contracts import ContractModel

AGENT_JOIN_TOKEN_FILE = "join-token"
AGENT_INSTALL_CONFIG_FILE = "agent-install.json"
DEFAULT_AGENT_BINARY = AGENT_NAME


class AgentServiceManager(StrEnum):
    Auto = "auto"
    Systemd = "systemd"
    Launchd = "launchd"


class AgentServiceScope(StrEnum):
    Auto = "auto"
    User = "user"
    System = "system"


class AgentInstallState(StrEnum):
    Planned = "planned"
    Installed = "installed"
    Unsupported = "unsupported"


class AgentInstallCommandResult(ContractModel):
    command: list[str]
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    ignored: bool = False


class AgentInstallRequest(ContractModel):
    name: str = "agent"
    pool: MachinePool = MachinePool("default")
    endpoint: str = "http://127.0.0.1:9000"
    join_token: str = ""
    version: str = "local"
    labels: dict[str, str] = Field(default_factory=dict)
    manager: AgentServiceManager = AgentServiceManager.Auto
    scope: AgentServiceScope = AgentServiceScope.Auto
    state_dir: str = DEFAULT_AGENT_STATE_DIR
    binary_path: str = DEFAULT_AGENT_BINARY
    worker_image: str = ""
    executor: str = "container"


class AgentInstallResult(ContractModel):
    state: AgentInstallState
    supported: bool
    dry_run: bool
    manager: AgentServiceManager
    scope: AgentServiceScope
    service_name: str
    state_dir: str
    token_path: str
    config_path: str
    command: list[str]
    service: RenderedServiceInstallPlan | None = None
    commands: list[AgentInstallCommandResult] = Field(default_factory=list)
    reason: str = ""


class AgentInstallRunner(Protocol):
    def which(self, name: str) -> str | None: ...

    def run(
        self, command: list[str], *, ignore_failure: bool = False
    ) -> AgentInstallCommandResult: ...


@dataclass(slots=True)
class SubprocessAgentInstallRunner:
    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def run(self, command: list[str], *, ignore_failure: bool = False) -> AgentInstallCommandResult:
        completed = subprocess.run(command, text=True, capture_output=True, check=False)
        result = AgentInstallCommandResult(
            command=command,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            ignored=ignore_failure and completed.returncode != 0,
        )
        if completed.returncode and not ignore_failure:
            msg = f"{command[0]} failed with exit code {completed.returncode}: {completed.stderr}"
            raise RuntimeError(msg)
        return result


@dataclass(slots=True)
class AgentInstallEnvironment:
    os_name: str = field(default_factory=lambda: sys.platform)
    uid: int = field(default_factory=os.getuid)
    home: Path = field(default_factory=Path.home)
    systemd_unit_dir: Path = Path(DEFAULT_SYSTEMD_UNIT_DIR)
    launchd_system_dir: Path = Path(DEFAULT_LAUNCHD_SYSTEM_DIR)

    @property
    def root(self) -> bool:
        return self.uid == 0


def plan_agent_service_install(
    request: AgentInstallRequest,
    *,
    environment: AgentInstallEnvironment | None = None,
    runner: AgentInstallRunner | None = None,
) -> AgentInstallResult:
    del runner
    env = environment or AgentInstallEnvironment()
    selected_manager = resolve_agent_service_manager(request.manager, env.os_name)
    if selected_manager is None:
        return _unsupported_result(request, env, "unsupported operating system")

    selected_scope = resolve_agent_service_scope(request.scope, env)
    service_name = normalize_service_name(f"{AGENT_NAME}-{request.name}")
    token_path = Path(request.state_dir) / AGENT_JOIN_TOKEN_FILE
    config_path = Path(request.state_dir) / AGENT_INSTALL_CONFIG_FILE
    command = build_agent_service_command(request, token_path=token_path)
    service = render_agent_service(
        request,
        command=command,
        service_name=service_name,
        scope=selected_scope,
        manager=selected_manager,
        environment=env,
    )
    return AgentInstallResult(
        state=AgentInstallState.Planned,
        supported=True,
        dry_run=True,
        manager=selected_manager,
        scope=selected_scope,
        service_name=service_name,
        state_dir=str(Path(request.state_dir)),
        token_path=str(token_path),
        config_path=str(config_path),
        command=command,
        service=service,
    )


def install_agent_service(
    request: AgentInstallRequest,
    *,
    dry_run: bool = False,
    environment: AgentInstallEnvironment | None = None,
    runner: AgentInstallRunner | None = None,
) -> AgentInstallResult:
    plan = plan_agent_service_install(request, environment=environment, runner=runner)
    if dry_run or not plan.supported or plan.service is None:
        return plan
    if not request.join_token:
        msg = "--join-token is required when installing an agent service"
        raise ValueError(msg)

    command_runner = runner or SubprocessAgentInstallRunner()
    command_name = "systemctl" if plan.manager is AgentServiceManager.Systemd else "launchctl"
    if command_runner.which(command_name) is None:
        return plan.model_copy(
            update={
                "state": AgentInstallState.Unsupported,
                "supported": False,
                "dry_run": True,
                "reason": f"{command_name} not found",
            }
        )

    state_dir = Path(plan.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    _write_private_file(Path(plan.token_path), request.join_token + "\n")
    _write_private_file(Path(plan.config_path), _install_config_json(request, plan) + "\n")

    service_path = Path(plan.service.target_path).expanduser()
    service_path.parent.mkdir(parents=True, exist_ok=True)
    _write_service_file(service_path, plan.service.content)

    results: list[AgentInstallCommandResult] = []
    for index, command in enumerate(plan.service.commands):
        results.append(
            command_runner.run(
                command,
                ignore_failure=plan.manager is AgentServiceManager.Launchd and index == 0,
            )
        )
    return plan.model_copy(
        update={
            "state": AgentInstallState.Installed,
            "dry_run": False,
            "commands": results,
        }
    )


def resolve_agent_service_manager(
    manager: AgentServiceManager,
    os_name: str,
) -> AgentServiceManager | None:
    if manager is not AgentServiceManager.Auto:
        return manager
    normalized_os = os_name.lower()
    if normalized_os == "darwin":
        return AgentServiceManager.Launchd
    if normalized_os.startswith("linux"):
        return AgentServiceManager.Systemd
    return None


def resolve_agent_service_scope(
    scope: AgentServiceScope,
    environment: AgentInstallEnvironment,
) -> AgentServiceScope:
    if scope is not AgentServiceScope.Auto:
        return scope
    return AgentServiceScope.System if environment.root else AgentServiceScope.User


def build_agent_service_command(request: AgentInstallRequest, *, token_path: Path) -> list[str]:
    command = [
        request.binary_path,
        "join",
        "--gateway",
        request.endpoint,
        "--join-token-file",
        str(token_path),
        "--state-dir",
        request.state_dir,
        "--hostname",
        request.name,
        "--executor",
        request.executor,
    ]
    if request.worker_image:
        command.extend(["--worker-image", request.worker_image])
    return command


def render_agent_service(
    request: AgentInstallRequest,
    *,
    command: list[str],
    service_name: str,
    scope: AgentServiceScope,
    manager: AgentServiceManager,
    environment: AgentInstallEnvironment,
) -> RenderedServiceInstallPlan:
    spec = AgentServiceSpec(
        name=service_name,
        description=AGENT_SERVICE_DESCRIPTION,
        binary_path=command[0],
        args=command[1:],
        state_dir=request.state_dir,
        env={
            "AGENT_STATE_DIR": request.state_dir,
            "AGENT_CONFIG": str(Path(request.state_dir) / AGENT_INSTALL_CONFIG_FILE),
        },
    )
    if manager is AgentServiceManager.Systemd:
        return _render_systemd_service(spec, scope=scope, environment=environment)
    return _render_launchd_service(spec, scope=scope, environment=environment)


def _render_systemd_service(
    spec: AgentServiceSpec,
    *,
    scope: AgentServiceScope,
    environment: AgentInstallEnvironment,
) -> RenderedServiceInstallPlan:
    unit_name = f"{normalize_service_name(spec.name)}.service"
    if scope is AgentServiceScope.System:
        target_path = environment.systemd_unit_dir / unit_name
        commands = [
            ["systemctl", "daemon-reload"],
            ["systemctl", "enable", unit_name],
            ["systemctl", "start", unit_name],
        ]
    else:
        target_path = environment.home / ".config" / "systemd" / "user" / unit_name
        commands = [
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", unit_name],
            ["systemctl", "--user", "start", unit_name],
        ]
    return RenderedServiceInstallPlan(
        platform=ServicePlatform.Systemd,
        spec=spec,
        target_path=str(target_path),
        content=render_systemd_unit(spec),
        commands=commands,
    )


def _render_launchd_service(
    spec: AgentServiceSpec,
    *,
    scope: AgentServiceScope,
    environment: AgentInstallEnvironment,
) -> RenderedServiceInstallPlan:
    label = launchd_label(spec.name)
    if scope is AgentServiceScope.System:
        target_path = environment.launchd_system_dir / f"{label}.plist"
        domain = "system"
    else:
        target_path = environment.home / DEFAULT_LAUNCHD_USER_DIR / f"{label}.plist"
        domain = f"gui/{environment.uid}"
    return RenderedServiceInstallPlan(
        platform=ServicePlatform.Launchd,
        spec=spec,
        target_path=str(target_path),
        content=render_launchd_plist(spec, root=scope is AgentServiceScope.System),
        commands=[
            ["launchctl", "bootout", domain, str(target_path)],
            ["launchctl", "bootstrap", domain, str(target_path)],
            ["launchctl", "kickstart", "-k", f"{domain}/{label}"],
        ],
    )


def _unsupported_result(
    request: AgentInstallRequest,
    environment: AgentInstallEnvironment,
    reason: str,
    manager: AgentServiceManager = AgentServiceManager.Auto,
) -> AgentInstallResult:
    token_path = Path(request.state_dir) / AGENT_JOIN_TOKEN_FILE
    config_path = Path(request.state_dir) / AGENT_INSTALL_CONFIG_FILE
    return AgentInstallResult(
        state=AgentInstallState.Unsupported,
        supported=False,
        dry_run=True,
        manager=manager,
        scope=resolve_agent_service_scope(request.scope, environment),
        service_name=normalize_service_name(f"{AGENT_NAME}-{request.name}"),
        state_dir=str(Path(request.state_dir)),
        token_path=str(token_path),
        config_path=str(config_path),
        command=build_agent_service_command(request, token_path=token_path),
        reason=reason,
    )


def _install_config_json(request: AgentInstallRequest, plan: AgentInstallResult) -> str:
    return json.dumps(
        {
            "name": request.name,
            "pool": request.pool,
            "endpoint": request.endpoint,
            "version": request.version,
            "labels": request.labels,
            "token_file": plan.token_path,
            "service_name": plan.service_name,
            "manager": plan.manager.value,
            "scope": plan.scope.value,
        },
        indent=2,
        sort_keys=True,
    )


def _write_private_file(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(contents)
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _write_service_file(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text(contents, encoding="utf-8")
    os.chmod(temp_path, 0o644)
    os.replace(temp_path, path)
