"""The one place a machine's lifecycle phase is allowed to move.

Every writer, whether the provider reconcile, the gateway's join and heartbeat
paths, a drain, or a removal, goes through `advance_machine_lifecycle`, so the
order phases may be visited in is decided once. Prepared machines can stop and
resume; a resumed machine must join before becoming ready again.
"""

from __future__ import annotations

from datetime import datetime

from database.repositories.compute import ComputeProviderInstanceRepository
from database.repositories.orchestration import MachineRepository
from database.types import DatabaseSession
from observability.workspace_changes import WorkspaceChangePublisher
from shared.compute_enrollment import MachineBootstrapFailureReason
from shared.compute_fleet import Machine, MachineLifecycle, ResourceStatus
from shared.errors import ConflictError
from shared.http.workspace_changes import WorkspaceChangeTopic, WorkspaceChangeType
from shared.timestamps import to_utc, utc_now
from sqlalchemy import event

from compute.offers import ReservationStatus

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
            MachineLifecycle.Stopping,
            MachineLifecycle.Stopped,
            MachineLifecycle.Draining,
            MachineLifecycle.Terminating,
        }
    ),
    # A restarted agent joins again from any phase it can still be running in;
    # the heartbeat that follows makes it ready. Refusing the join would leave a
    # draining host with no way back once its drain is over.
    MachineLifecycle.Ready: frozenset(
        {
            MachineLifecycle.Joining,
            MachineLifecycle.Stopping,
            MachineLifecycle.Draining,
            MachineLifecycle.Terminating,
        }
    ),
    MachineLifecycle.Draining: frozenset(
        {MachineLifecycle.Joining, MachineLifecycle.Stopping, MachineLifecycle.Terminating}
    ),
    # A pass can observe a reserve stopped and resume it at once, so the first
    # phase recorded after `stopping` may be `resuming`. One the provider reports
    # active never stopped, and joins again from here.
    MachineLifecycle.Stopping: frozenset(
        {
            MachineLifecycle.Stopped,
            MachineLifecycle.Resuming,
            MachineLifecycle.Joining,
            MachineLifecycle.Terminating,
        }
    ),
    MachineLifecycle.Stopped: frozenset(
        {MachineLifecycle.Resuming, MachineLifecycle.Joining, MachineLifecycle.Terminating}
    ),
    MachineLifecycle.Resuming: frozenset(
        {MachineLifecycle.Joining, MachineLifecycle.Stopping, MachineLifecycle.Terminating}
    ),
    MachineLifecycle.Terminating: frozenset({MachineLifecycle.Joining}),
    # A host that failed its preflight is fixed by its operator and joins again.
    MachineLifecycle.Failed: frozenset({MachineLifecycle.Joining, MachineLifecycle.Terminating}),
    MachineLifecycle.Deleted: frozenset(),
}

_TERMINAL_FROM_ANYWHERE = frozenset({MachineLifecycle.Failed, MachineLifecycle.Deleted})

RETAINED_MACHINE_LIFECYCLES = frozenset(
    {MachineLifecycle.Stopping, MachineLifecycle.Stopped, MachineLifecycle.Resuming}
)
"""Phases of a reserve its stream has not yet authorized to serve.

While the provider row is not active, only that authorization moves a resumed
reserve on to `joining`, and the agent's own report of joining leaves these
phases alone.
"""


_PREPARED_RESERVE_STATUSES = frozenset(
    {
        ReservationStatus.Preparing.value,
        ReservationStatus.Stopping.value,
        ReservationStatus.Stopped.value,
    }
)


def reserve_awaits_resume(session: DatabaseSession, *, machine_id: str, workspace_id: str) -> bool:
    """Whether a worker on this machine must wait for its reserve's resume to be authorized.

    A reserve runs its worker before it may serve: while it is prepared to
    hibernate, and from boot on a resume. This is what keeps work off that
    worker until the stream authorizes the resume. A machine left in a retained
    phase by an unobserved stop serves once its provider row is active.
    """
    phases = ComputeProviderInstanceRepository(session).machine_reserve_phases(
        machine_id, workspace_id=workspace_id
    )
    if phases is None:
        return True
    lifecycle, status = MachineLifecycle(phases[0]), phases[1]
    if status in _PREPARED_RESERVE_STATUSES:
        return True
    if lifecycle not in RETAINED_MACHINE_LIFECYCLES:
        return False
    return status != ReservationStatus.Active.value


_DEFAULT_MESSAGES: dict[MachineLifecycle, str] = {
    MachineLifecycle.Requested: "Waiting for the machine to be launched",
    MachineLifecycle.Provisioning: "Instance is starting; waiting for the node to report",
    MachineLifecycle.Booting: "Node is installing the agent",
    MachineLifecycle.Joining: "Agent joined; waiting for its first heartbeat",
    MachineLifecycle.Ready: "Ready for workloads",
    MachineLifecycle.Draining: "Draining; no new work is placed here",
    MachineLifecycle.Stopping: "Stopping prepared capacity",
    MachineLifecycle.Stopped: "Prepared capacity is stopped",
    MachineLifecycle.Resuming: "Resuming prepared capacity",
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
    MachineLifecycle.Stopping: ResourceStatus.Stopped,
    MachineLifecycle.Stopped: ResourceStatus.Stopped,
    MachineLifecycle.Resuming: ResourceStatus.Created,
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
    deleting_workspace_id: str | None = None,
    message: str = "",
    failure: MachineBootstrapFailureReason | None = None,
    now: datetime | None = None,
) -> Machine:
    """Advance and persist in the caller's transaction, telling its workspaces on commit.

    Nothing is published when the phase, message and failure all stay as they
    were: a heartbeat re-stating `ready` is not a change. `deleting_workspace_id`
    names the workspace a deletion pass is tearing down, whose rows can no
    longer be written through the active-owner fence.
    """
    advanced = advance_machine_lifecycle(
        machine, lifecycle, message=message, failure=failure, now=now
    )
    repository = MachineRepository(session)
    updated = repository.upsert(
        advanced,
        workspace_id=deleting_workspace_id or workspace_id,
        for_workspace_deletion=deleting_workspace_id is not None,
    )
    unchanged = (
        advanced.lifecycle is machine.lifecycle
        and advanced.lifecycle_message == machine.lifecycle_message
        and advanced.lifecycle_failure is machine.lifecycle_failure
    )
    if workspace_changes is None or unchanged:
        return updated
    anchor_workspace_id = (
        deleting_workspace_id or workspace_id or repository.workspace_id(updated.id)
    )
    # Every workspace the machine serves reads it, and each subscribes only to
    # its own stream; a platform or connection node serves no named workspace
    # and reaches the rest of the account through the panels' slow refetch.
    served: dict[str, None] = dict.fromkeys(
        [*([anchor_workspace_id] if anchor_workspace_id else []), *updated.workspace_ids]
    )
    change = (
        WorkspaceChangeType.Deleted
        if lifecycle is MachineLifecycle.Deleted
        else WorkspaceChangeType.Updated
    )
    for served_workspace_id in served:
        publish_machine_change_on_commit(
            session,
            workspace_changes,
            workspace_id=served_workspace_id,
            machine_id=updated.id,
            change=change,
        )
    return updated


_PENDING_MACHINE_CHANGES = "pending_machine_changes"


def publish_machine_change_on_commit(
    session: DatabaseSession,
    workspace_changes: WorkspaceChangePublisher,
    *,
    workspace_id: str,
    machine_id: str,
    change: WorkspaceChangeType,
) -> None:
    """Queue one change on the session; it is sent after commit and dropped on rollback.

    The queue lives on the session rather than on a one-shot listener because a
    listener outlives the transaction that registered it: registered, rolled
    back, and left in place, it would fire on the session's next commit for a
    row that never landed.
    """
    pending: list[tuple[str, str, WorkspaceChangeType]] | None = session.info.get(
        _PENDING_MACHINE_CHANGES
    )
    if pending is None:
        pending = []
        session.info[_PENDING_MACHINE_CHANGES] = pending

        def publish(_session: DatabaseSession) -> None:
            queued = list(pending)
            pending.clear()
            for queued_workspace_id, queued_machine_id, queued_change in queued:
                workspace_changes.emit_change(
                    workspace_id=queued_workspace_id,
                    topic=WorkspaceChangeTopic.ComputeMachines,
                    change=queued_change,
                    resource_id=queued_machine_id,
                )

        def discard(_session: DatabaseSession) -> None:
            pending.clear()

        event.listen(session, "after_commit", publish)
        event.listen(session, "after_rollback", discard)
    pending.append((workspace_id, machine_id, change))


__all__ = [
    "MACHINE_LIFECYCLE_TRANSITIONS",
    "RETAINED_MACHINE_LIFECYCLES",
    "advance_machine_lifecycle",
    "lifecycle_failure_message",
    "machine_lifecycle_allowed",
    "publish_machine_change_on_commit",
    "reserve_awaits_resume",
    "write_machine_lifecycle",
]
