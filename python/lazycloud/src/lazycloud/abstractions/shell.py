from __future__ import annotations

import time
from dataclasses import dataclass
from uuid import UUID

from typing_extensions import Never

from lazycloud.abstractions.workspace_sync import ContainerWorkspaceSyncer, sync_local_workspace
from lazycloud.clients.api import ApiError
from lazycloud.clients.workloads import CONNECT_WAIT_SECONDS, WorkloadsClient
from lazycloud.contracts.api import CreateInstanceRequest, ErrorCode
from lazycloud.control import ControlClientConfig, resolve_control_client_config, workloads_client
from lazycloud.session.interrupts import uninterrupted
from lazycloud.terminal_shell import InteractiveShell, ShellConnectionError

# How long a shell waits for its container to start, image pull included.
SHELL_READY_TIMEOUT_SECONDS = 600.0
# How long a shell waits between asks while its container is still starting.
SHELL_READY_RETRY_SECONDS = 0.5


@dataclass(frozen=True, slots=True)
class ShellSession:
    container_id: str
    stub_id: str
    sync_dir: str | None = None
    # Started by create_standalone; Ctrl-C before the shell attaches stops it.
    standalone: bool = False


@dataclass(slots=True)
class Shell:
    client: WorkloadsClient | None = None
    workspace: str | None = None
    endpoint: str | None = None
    token: str | None = None
    timeout_seconds: float = 10.0
    interactive_shell: InteractiveShell | None = None
    ready_timeout_seconds: float = SHELL_READY_TIMEOUT_SECONDS
    ready_retry_seconds: float = SHELL_READY_RETRY_SECONDS

    @property
    def control_client(self) -> WorkloadsClient:
        if self.client is None:
            self.client = workloads_client(self._config())
        return self.client

    def create_standalone(self, stub_id: str, *, sync_dir: str | None = None) -> ShellSession:
        """Start an idle container of the release for shells; it stops after the last closes.

        Ctrl-C before a shell attaches stops the container.
        """
        client = self.control_client
        # Ctrl-C waits for the request, so a container it created is stopped.
        instance, interrupted = uninterrupted(
            lambda: client.create_instance(
                CreateInstanceRequest(release_id=UUID(stub_id), shell=True)
            )
        )
        session = ShellSession(
            container_id=str(instance.id),
            stub_id=str(instance.release_id),
            sync_dir=sync_dir,
            standalone=True,
        )
        try:
            if interrupted is not None:
                raise interrupted
            self._sync_directory(session)
        except KeyboardInterrupt as exc:
            self._stop_interrupted(session, exc)
        return session

    def create_existing(self, container_id: str, *, sync_dir: str | None = None) -> ShellSession:
        client = self.control_client
        container = client.api.get_container(client.workspace, UUID(container_id))
        session = ShellSession(
            container_id=container_id, stub_id=str(container.release_id), sync_dir=sync_dir
        )
        self._sync_directory(session)
        return session

    def connect(self, session: ShellSession) -> int:
        """Open an interactive shell in the session's container; returns its exit code."""
        client = self.control_client
        container_id = UUID(session.container_id)
        try:
            wait_until_ready(
                client,
                container_id,
                timeout_seconds=self.ready_timeout_seconds,
                retry_seconds=self.ready_retry_seconds,
            )
        except KeyboardInterrupt as exc:
            if session.standalone:
                self._stop_interrupted(session, exc)
            raise
        terminal = self.interactive_shell or InteractiveShell()
        syncer = (
            ContainerWorkspaceSyncer(
                container_id=session.container_id, local_dir=session.sync_dir, client=client
            )
            if session.sync_dir
            else None
        )
        if syncer is not None:
            syncer.start()
        try:
            result = terminal.run(
                url=lambda cols, rows, term: client.shell_url(
                    container_id, cols=cols, rows=rows, term=term
                ),
                token=client.api.token,
                open_timeout_seconds=self._config().timeout_seconds,
                check_health=syncer.raise_if_failed if syncer is not None else None,
            )
        finally:
            if syncer is not None:
                syncer.stop()
        if syncer is not None:
            syncer.raise_if_failed()
        return result

    def _sync_directory(self, session: ShellSession) -> None:
        if session.sync_dir:
            client = self.control_client
            wait_until_ready(
                client,
                UUID(session.container_id),
                timeout_seconds=self.ready_timeout_seconds,
                retry_seconds=self.ready_retry_seconds,
            )
            sync_local_workspace(
                container_id=session.container_id, local_dir=session.sync_dir, client=client
            )

    def _stop_interrupted(self, session: ShellSession, interruption: KeyboardInterrupt) -> Never:
        client = self.control_client
        try:
            _, again = uninterrupted(
                lambda: client.api.stop_container(client.workspace, UUID(session.container_id))
            )
        except Exception as cleanup_error:
            raise interruption from cleanup_error
        raise again or interruption

    def _config(self) -> ControlClientConfig:
        return resolve_control_client_config(
            workspace=self.workspace,
            endpoint=self.endpoint,
            token=self.token,
            timeout_seconds=self.timeout_seconds,
        )


def wait_until_ready(
    client: WorkloadsClient,
    container_id: UUID,
    *,
    timeout_seconds: float,
    retry_seconds: float,
) -> None:
    """Hold until the container is ready; a stopped container or the deadline raises."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        remaining = deadline - time.monotonic()
        try:
            client.connect(
                container_id, wait_seconds=max(0, int(min(CONNECT_WAIT_SECONDS, remaining)))
            )
        except ApiError as exc:
            if exc.code is ErrorCode.conflict:
                msg = f"container {container_id} stopped: {exc.message}"
                raise ShellConnectionError(msg) from exc
            if exc.code is not ErrorCode.unavailable:
                raise
            if deadline - time.monotonic() <= 0:
                msg = (
                    f"container {container_id} did not become ready within "
                    f"{timeout_seconds:g} seconds: {exc.message}"
                )
                raise ShellConnectionError(msg) from exc
            time.sleep(min(retry_seconds, max(0.0, deadline - time.monotonic())))
            continue
        return


__all__ = ["Shell", "ShellSession", "wait_until_ready"]
