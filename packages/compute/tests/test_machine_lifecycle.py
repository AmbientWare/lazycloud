from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from compute.machine_lifecycle import advance_machine_lifecycle
from shared.compute_enrollment import MachineBootstrapFailureReason
from shared.compute_fleet import Machine, MachineLifecycle, ResourceStatus
from shared.errors import ConflictError


def test_lifecycle_moves_forward_to_ready_and_refuses_a_backwards_move() -> None:
    started = datetime(2026, 1, 1, tzinfo=UTC)
    machine = Machine(id="m", lifecycle=MachineLifecycle.Joining, lifecycle_at=started)

    ready = advance_machine_lifecycle(
        machine, MachineLifecycle.Ready, now=started + timedelta(minutes=1)
    )
    assert ready.lifecycle is MachineLifecycle.Ready
    assert ready.status is ResourceStatus.Running
    assert ready.lifecycle_at == started + timedelta(minutes=1)

    # A heartbeat re-stating ready keeps the phase clock a reclaim measures from.
    again = advance_machine_lifecycle(
        ready, MachineLifecycle.Ready, now=started + timedelta(minutes=5)
    )
    assert again.lifecycle_at == ready.lifecycle_at

    with pytest.raises(ConflictError):
        advance_machine_lifecycle(ready, MachineLifecycle.Booting)

    # Failure is reachable from anywhere and its reason survives termination.
    failed = advance_machine_lifecycle(
        ready,
        MachineLifecycle.Failed,
        failure=MachineBootstrapFailureReason.ServiceLost,
    )
    terminating = advance_machine_lifecycle(failed, MachineLifecycle.Terminating)
    assert terminating.lifecycle_failure is MachineBootstrapFailureReason.ServiceLost
    deleted = advance_machine_lifecycle(terminating, MachineLifecycle.Deleted)
    assert deleted.status is ResourceStatus.Deleted
    with pytest.raises(ConflictError):
        advance_machine_lifecycle(deleted, MachineLifecycle.Failed)
