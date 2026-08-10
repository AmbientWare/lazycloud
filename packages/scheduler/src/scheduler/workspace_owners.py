from __future__ import annotations

from dataclasses import dataclass

from database.repositories.identity import WorkspaceMemberRepository

from scheduler.services import SchedulerContext


@dataclass(frozen=True, slots=True)
class DatabaseWorkspaceOwners:
    """Resolve which account owns a workspace, for private-capacity placement.

    Read rather than cached: an owner change has to take effect on the next
    placement decision, because a stale answer is capacity serving a tenant that no
    longer owns it. An unowned workspace answers empty, which places nothing on
    private capacity rather than placing it anywhere.
    """

    context: SchedulerContext

    def owner_user_id(self, workspace_id: str) -> str:
        with self.context.database.session() as session:
            owner = WorkspaceMemberRepository(session).owner(workspace_id)
        return owner.user_id if owner is not None else ""


__all__ = ["DatabaseWorkspaceOwners"]
