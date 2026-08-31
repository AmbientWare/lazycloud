from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from shared.timestamps import utc_now

from worker.credential_payloads import WorkerCredentialPrincipal
from worker.tools import (
    ContainerCredentialRequest,
    ContainerCredentials,
    WorkspaceStorageCredentials,
)

LOGGER = logging.getLogger(__name__)

REFRESH_AT_FRACTION = 0.5
"""How much of a credential's life may pass before it is replaced.

Half, so a refresh that fails still has the same span again to succeed in before
anything stops working. Renewing at the last moment turns one failed call into a
broken mount.
"""


@dataclass(frozen=True, slots=True)
class WorkspaceCredentialWindow:
    """When a mount's credential was written and when it stops working."""

    issued_at: datetime
    expires_at: datetime | None

    def due(self, *, now: datetime | None = None) -> bool:
        """Whether enough of this credential's life has passed to replace it.

        A credential with no expiry is never due: the store cannot say when it
        stops working, so there is no moment to act on.
        """
        if self.expires_at is None:
            return False
        lifetime = self.expires_at - self.issued_at
        if lifetime <= timedelta(0):
            return True
        return (now or utc_now()) >= self.issued_at + lifetime * REFRESH_AT_FRACTION


class WorkspaceCredentialVendor(Protocol):
    def vend(
        self,
        request: ContainerCredentialRequest,
        *,
        principal: WorkerCredentialPrincipal,
    ) -> ContainerCredentials: ...


class MountedWorkspaceInstance(Protocol):
    """A running container, which is what makes a workspace's mount refreshable.

    Credentials are vended against a container the control plane agrees this
    worker is running, so a mount with no live container has nothing to ask with —
    and needs nothing, because it is about to be cleaned up.
    """

    @property
    def workspace_id(self) -> str: ...

    @property
    def workspace_name(self) -> str: ...

    @property
    def stub_id(self) -> str: ...

    @property
    def container_id(self) -> str: ...


class MountedWorkspaceInstanceLister(Protocol):
    def list_container_instances(self) -> Sequence[MountedWorkspaceInstance]: ...


class RefreshableWorkspaceStorage(Protocol):
    def workspaces_due_for_refresh(self, *, now: datetime | None = ...) -> list[str]: ...

    def refresh_credentials(
        self,
        workspace_name: str,
        credentials: WorkspaceStorageCredentials,
    ) -> None: ...


@dataclass(slots=True)
class WorkspaceCredentialRefresher:
    """Keeps a live mount's credential current.

    The mount reads its credential from a file, so refreshing is a write rather
    than a remount: nothing holding a file handle is disturbed, and a container
    running longer than one credential's life keeps working.
    """

    storage: RefreshableWorkspaceStorage
    instances: MountedWorkspaceInstanceLister
    credentials: WorkspaceCredentialVendor
    principal: WorkerCredentialPrincipal

    def refresh_due(self, *, now: datetime | None = None) -> list[str]:
        """Replace every mounted workspace's credential that is due, and say which."""
        due = self.storage.workspaces_due_for_refresh(now=now)
        if not due:
            return []
        by_workspace = self._instances_by_workspace()
        refreshed: list[str] = []
        for workspace_name in due:
            instance = by_workspace.get(workspace_name)
            if instance is None:
                continue
            try:
                vended = self.credentials.vend(
                    ContainerCredentialRequest(
                        workspace_id=instance.workspace_id,
                        stub_id=instance.stub_id,
                        container_id=instance.container_id,
                        workspace_storage=True,
                    ),
                    principal=self.principal,
                )
            except Exception:
                # Reported and retried next pass rather than raised: what this
                # would have replaced is still valid for the rest of its life, so
                # one failed call is not yet a broken mount.
                LOGGER.warning(
                    "workspace storage credential refresh failed for %s",
                    workspace_name,
                    exc_info=True,
                )
                continue
            if vended.workspace_storage is None:
                continue
            self.storage.refresh_credentials(workspace_name, vended.workspace_storage)
            refreshed.append(workspace_name)
        return refreshed

    def _instances_by_workspace(self) -> dict[str, MountedWorkspaceInstance]:
        by_workspace: dict[str, MountedWorkspaceInstance] = {}
        for instance in self.instances.list_container_instances():
            if instance.workspace_name and instance.workspace_name not in by_workspace:
                by_workspace[instance.workspace_name] = instance
        return by_workspace


__all__ = [
    "REFRESH_AT_FRACTION",
    "WorkspaceCredentialRefresher",
    "WorkspaceCredentialWindow",
]
