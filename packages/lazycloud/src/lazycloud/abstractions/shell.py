from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from shared.http.shells import (
    CreateShellInExistingContainerResponse,
    CreateStandaloneShellResponse,
    ShellConnectPlanResponse,
)

from lazycloud.abstractions.serve import ContainerWorkspaceSyncer, sync_local_workspace
from lazycloud.control import ControlClientConfig, resolve_control_client_config
from lazycloud.control_clients import gateway_control_client
from lazycloud.terminal_shell import InteractiveShell


class ShellClient(Protocol):
    def create_standalone(self, stub_id: str) -> CreateStandaloneShellResponse: ...

    def create_existing(self, container_id: str) -> CreateShellInExistingContainerResponse: ...

    def connect_plan(self, stub_id: str, container_id: str) -> ShellConnectPlanResponse: ...


@dataclass(frozen=True, slots=True)
class ShellSession:
    container_id: str
    stub_id: str
    username: str
    password: str = field(repr=False)
    sync_dir: str | None = None


@dataclass(slots=True)
class Shell:
    client: ShellClient | None = None
    workspace: str | None = None
    endpoint: str | None = None
    token: str | None = None
    timeout_seconds: float = 10.0
    interactive_shell: InteractiveShell | None = None

    @property
    def control_client(self) -> ShellClient:
        if self.client is None:
            config = resolve_control_client_config(
                workspace=self.workspace,
                endpoint=self.endpoint,
                token=self.token,
                timeout_seconds=self.timeout_seconds,
            )
            self.client = _default_shell_client(config)
        return self.client

    def create_standalone(self, stub_id: str, *, sync_dir: str | None = None) -> ShellSession:
        response = self.control_client.create_standalone(stub_id)
        session = ShellSession(
            container_id=response.container_id,
            stub_id=stub_id,
            username=response.username,
            password=response.password,
            sync_dir=sync_dir,
        )
        self._sync_directory(session)
        return session

    def create_existing(self, container_id: str, *, sync_dir: str | None = None) -> ShellSession:
        response = self.control_client.create_existing(container_id)
        session = ShellSession(
            container_id=container_id,
            stub_id=response.stub_id,
            username=response.username,
            password=response.password,
            sync_dir=sync_dir,
        )
        self._sync_directory(session)
        return session

    def _sync_directory(self, session: ShellSession) -> None:
        if session.sync_dir:
            config = resolve_control_client_config(
                workspace=self.workspace,
                endpoint=self.endpoint,
                token=self.token,
                timeout_seconds=self.timeout_seconds,
            )
            sync_local_workspace(
                container_id=session.container_id,
                local_dir=session.sync_dir,
                gateway_client=gateway_control_client(config),
            )

    def connect_plan(self, stub_id: str, container_id: str) -> ShellConnectPlanResponse:
        return self.control_client.connect_plan(stub_id, container_id)

    def connect(self, session: ShellSession) -> int:
        config = resolve_control_client_config(
            workspace=self.workspace,
            endpoint=self.endpoint,
            token=self.token,
            timeout_seconds=self.timeout_seconds,
        )
        plan = self.connect_plan(session.stub_id, session.container_id)
        terminal = self.interactive_shell or InteractiveShell()
        syncer = (
            ContainerWorkspaceSyncer(
                container_id=session.container_id,
                local_dir=session.sync_dir,
                gateway_client=gateway_control_client(config),
            )
            if session.sync_dir
            else None
        )
        if syncer is not None:
            syncer.start()
        try:
            result = terminal.run(
                endpoint=config.endpoint,
                workspace=config.workspace,
                token=config.token,
                credentials=session,
                plan=plan,
                open_timeout_seconds=config.timeout_seconds,
                check_health=syncer.raise_if_failed if syncer is not None else None,
            )
        finally:
            if syncer is not None:
                syncer.stop()
        if syncer is not None:
            syncer.raise_if_failed()
        return result


def _default_shell_client(config: ControlClientConfig) -> ShellClient:
    from lazycloud.clients.shell.control import ShellControlClient

    return ShellControlClient.from_endpoint(
        config.endpoint,
        token=config.token,
        timeout_seconds=config.timeout_seconds,
        workspace=config.workspace,
    )


__all__ = [
    "Shell",
    "ShellClient",
    "ShellSession",
]
