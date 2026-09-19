"""The one place a machine's lifecycle phase is allowed to move.

Every writer, whether the provider reconcile, the gateway's join and heartbeat
paths, a drain, or a removal, goes through `advance_machine_lifecycle`, so the
order phases may be visited in is decided once. A phase may only move forward;
`failed` and `deleted` are reachable from anywhere, and a fresh join is the one
way back from `failed` or `ready` into `joining`.
"""

from __future__ import annotations

from datetime import datetime

from database.repositories.orchestration import MachineRepository
from database.types import DatabaseSession
from observability.workspace_changes import WorkspaceChangePublisher
from shared.compute_enrollment import MachineBootstrapFailureReason
from shared.compute_fleet import Machine, MachineLifecycle, ResourceStatus
from shared.errors import ConflictError
from shared.http.workspace_changes import WorkspaceChangeTopic, WorkspaceChangeType
from shared.timestamps import to_utc, utc_now
from sqlalchemy import event

MACHINE_LIFECYCLE_TRANSITIONS: dict[MachineLifecycle, frozenset[MachineLifecycle]] = {
    MachineLifecycle.Requested: frozenset(
        {
            MachineLifecycle.Provisioning,
            MachineLifecycle.Booting,
            MachineLifecycle.Joining,
            MachineLifecycle.Draining,
            MachineLifecycle.Terminating,
        }
    ),
    MachineLifecycle.Provisioning: frozenset(
        {
            MachineLifecycle.Booting,
            MachineLifecycle.Joining,
            MachineLifecycle.Draining,
            MachineLifecycle.Terminating,
        }
    ),
    MachineLifecycle.Booting: frozenset(
        {
            MachineLifecycle.Joining,
            MachineLifecycle.Draining,
            MachineLifecycle.Terminating,
        }
    ),
    MachineLifecycle.Joining: frozenset(
        {
            MachineLifecycle.Ready,
            MachineLifecycle.Draining,
            MachineLifecycle.Terminating,
        }
    ),
    # A restarted agent joins again; the heartbeat that follows makes it ready.
    MachineLifecycle.Ready: frozenset(
        {
            MachineLifecycle.Joining,
            MachineLifecycle.Draining,
            MachineLifecycle.Terminating,
        }
    ),
    MachineLifecycle.Draining: frozenset({MachineLifecycle.Terminating}),
    MachineLifecycle.Terminating: frozenset(),
    # A host that failed its preflight is fixed by its operator and joins again.
    MachineLifecycle.Failed: frozenset({MachineLifecycle.Joining, MachineLifecycle.Terminating}),
    MachineLifecycle.Deleted: frozenset(),
}

_TERMINAL_FROM_ANYWHERE = frozenset({MachineLifecycle.Failed, MachineLifecycle.Deleted})

_DEFAULT_MESSAGES: dict[MachineLifecycle, str] = {
    MachineLifecycle.Requested: "Waiting for the machine to be launched",
    MachineLifecycle.Provisioning: "Instance is starting; waiting for the node to report",
    MachineLifecycle.Booting: "Node is installing the agent",
    MachineLifecycle.Joining: "Agent joined; waiting for its first heartbeat",
    MachineLifecycle.Ready: "Ready for workloads",
    MachineLifecycle.Draining: "Draining; no new work is placed here",
    MachineLifecycle.Terminating: "Shutting down",
    MachineLifecycle.Deleted: "Removed",
    MachineLifecycle.Failed: "Failed",
}

_RESOURCE_STATUS: dict[MachineLifecycle, ResourceStatus] = {
    MachineLifecycle.Requested: ResourceStatus.Created,
    MachineLifecycle.Provisioning: ResourceStatus.Created,
    MachineLifecycle.Booting: ResourceStatus.Created,
    MachineLifecycle.Joining: ResourceStatus.Created,
    MachineLifecycle.Ready: ResourceStatus.Running,
    MachineLifecycle.Draining: ResourceStatus.Stopped,
    MachineLifecycle.Terminating: ResourceStatus.Stopped,
    MachineLifecycle.Deleted: ResourceStatus.Deleted,
    MachineLifecycle.Failed: ResourceStatus.Failed,
}


def lifecycle_failure_message(failure: MachineBootstrapFailureReason) -> str:
    return failure.value.replace("_", " ").capitalize()


def machine_lifecycle_allowed(current: MachineLifecycle, target: MachineLifecycle) -> bool:
    if current is target:
        return True
    if target in _TERMINAL_FROM_ANYWHERE:
        return current is not MachineLifecycle.Deleted
    return target in MACHINE_LIFECYCLE_TRANSITIONS[current]


def advance_machine_lifecycle(
    machine: Machine,
    lifecycle: MachineLifecycle,
    *,
    message: str = "",
    failure: MachineBootstrapFailureReason | None = None,
    now: datetime | None = None,
) -> Machine:
    """The machine as it stands after moving to `lifecycle`, or a conflict.

    Writing the same phase again only refreshes the message and keeps
    `lifecycle_at`, so a heartbeat re-stating `ready` does not restart the clock
    a reclaim measures from. A failure reason travels with the machine into
    `terminating` and `deleted` so the reason it was taken away stays readable.
    """
    if lifecycle is MachineLifecycle.Failed and failure is None:
        failure = machine.lifecycle_failure or MachineBootstrapFailureReason.Unknown
    if not machine_lifecycle_allowed(machine.lifecycle, lifecycle):
        raise ConflictError(
            f"machine {machine.id} cannot move from {machine.lifecycle.value} to {lifecycle.value}"
        )
    current_time = utc_now() if now is None else to_utc(now)
    changed = lifecycle is not machine.lifecycle
    if failure is None and lifecycle in {
        MachineLifecycle.Terminating,
        MachineLifecycle.Deleted,
    }:
        failure = machine.lifecycle_failure
    if not message:
        if failure is not None and lifecycle is MachineLifecycle.Failed:
            message = lifecycle_failure_message(failure)
        elif not changed and machine.lifecycle_message:
            message = machine.lifecycle_message
        else:
            message = _DEFAULT_MESSAGES[lifecycle]
    return machine.model_copy(
        update={
            "lifecycle": lifecycle,
            "lifecycle_message": message[:512],
            "lifecycle_failure": failure,
            "lifecycle_at": current_time if changed else machine.lifecycle_at,
            "status": _RESOURCE_STATUS[lifecycle],
            "updated_at": current_time,
        }
    )


def write_machine_lifecycle(
    session: DatabaseSession,
    machine: Machine,
    lifecycle: MachineLifecycle,
    *,
    workspace_changes: WorkspaceChangePublisher | None,
    workspace_id: str | None = None,
    message: str = "",
    failure: MachineBootstrapFailureReason | None = None,
    now: datetime | None = None,
) -> Machine:
    """Advance and persist in the caller's transaction, telling the workspace on commit.

    The change stream is not part of the database transaction, so the event is
    queued on the session and sent only once the row it describes has
    committed; a rolled-back write publishes nothing.
    """
    repository = MachineRepository(session)
    updated = repository.upsert(
        advance_machine_lifecycle(machine, lifecycle, message=message, failure=failure, now=now),
        workspace_id=workspace_id,
    )
    owner_workspace_id = workspace_id or repository.workspace_id(updated.id)
    if workspace_changes is not None and owner_workspace_id is not None:
        publish_machine_change_on_commit(
            session,
            workspace_changes,
            workspace_id=owner_workspace_id,
            machine_id=updated.id,
            change=(
                WorkspaceChangeType.Deleted
                if lifecycle is MachineLifecycle.Deleted
                else WorkspaceChangeType.Updated
            ),
        )
    return updated


def publish_machine_change_on_commit(
    session: DatabaseSession,
    workspace_changes: WorkspaceChangePublisher,
    *,
    workspace_id: str,
    machine_id: str,
    change: WorkspaceChangeType,
) -> None:
    def publish(_session: DatabaseSession) -> None:
        workspace_changes.emit_change(
            workspace_id=workspace_id,
            topic=WorkspaceChangeTopic.ComputeMachines,
            change=change,
            resource_id=machine_id,
        )

    event.listen(session, "after_commit", publish, once=True)


__all__ = [
    "MACHINE_LIFECYCLE_TRANSITIONS",
    "advance_machine_lifecycle",
    "lifecycle_failure_message",
    "machine_lifecycle_allowed",
    "publish_machine_change_on_commit",
    "write_machine_lifecycle",
]
