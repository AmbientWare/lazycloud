from __future__ import annotations

from api.server.services import ApiServices
from control.service import ControlPlaneService
from database.repositories.execution import TaskRepository
from database.repositories.orchestration import AutoscalingTargetRepository, ContainerRepository
from execution.containers.preemption import PreemptedContainerService
from shared.container_requests import StopContainerReason
from shared.containers import ContainerRecord, ContainerStatus
from shared.deployments import StubKind
from shared.tasks import RetryPolicy, Task, TaskStatus
from shared.timestamps import utc_now


def _running_task(
    services: ApiServices,
    *,
    kind: StubKind,
    name: str,
) -> tuple[Task, ContainerRecord]:
    """One task a container is running, shaped the way production shapes it.

    A pooled function container carries no `task_id` of its own — it is started
    for its stub and what it is running is recorded only by the claim. Setting
    one here would let preemption resolve the task through a field production
    leaves empty, and the claim-side lookup that actually runs would go untested.
    """

    control = ControlPlaneService(services.context)
    stub = control.create_stub(name, kind=kind)
    task = services.tasks.create(
        name,
        workspace_id=stub.workspace_id,
        stub_id=stub.id,
        retry_policy=RetryPolicy(max_attempts=2),
    )
    with services.context.database.session() as session:
        container = ContainerRepository(session).records.create(
            {
                "name": name,
                "image": "image-preemption-test",
                "command": ["python", "-m", "runner"],
                "workspace_id": stub.workspace_id,
                "stub_id": stub.id,
                **({} if kind is StubKind.Function else {"task_id": task.id}),
                "status": ContainerStatus.Running.value,
            },
            workspace_id=stub.workspace_id,
            name=name,
            status=ContainerStatus.Running.value,
        )
    # Started through the service rather than by hand, so the attempt row exists.
    # It is the durable record that this container ran this task, and settling a
    # preemption twice resolves the task through it once the claim is cleared.
    task = services.tasks.start(task.id, container_id=container.id)
    return task, container


def test_function_preemption_uses_explicit_retry_policy(
    isolated_services: ApiServices,
) -> None:
    task, container = _running_task(
        isolated_services,
        kind=StubKind.Function,
        name="preempted-function",
    )
    service = PreemptedContainerService(
        services=isolated_services,
        stubs=isolated_services.control_plane_service,
    )

    result = service.preempted(container, exit_code=562)
    duplicate = service.preempted(container, exit_code=562)

    assert result.task_id == task.id
    assert result.status is TaskStatus.Retry
    assert result.changed
    assert result.retry_scheduled
    assert duplicate.status is TaskStatus.Retry
    assert not duplicate.changed
    assert not duplicate.retry_scheduled


def test_platform_stop_reactivates_retired_function_demand(
    isolated_services: ApiServices,
) -> None:
    task, container = _running_task(
        isolated_services, kind=StubKind.Function, name="released-function-demand"
    )
    with isolated_services.context.database.session() as session:
        TaskRepository(session).mark_claimable(task.id, at=utc_now())
        targets = AutoscalingTargetRepository(session)
        for claim in targets.claim_due(limit=10, lease_seconds=30):
            targets.complete(claim, next_reconcile_at=None)

    isolated_services.containers.stop(container.id, reason=StopContainerReason.Preempted)

    with isolated_services.context.database.session() as session:
        claims = AutoscalingTargetRepository(session).claim_due(limit=10, lease_seconds=30)
        assert [claim.stub_id for claim in claims] == [task.stub_id]
        released = TaskRepository(session).get(task.id, workspace_id=container.workspace_id)
        assert released is not None and released.status is TaskStatus.Pending
        assert released.container_id is None and released.claimable_at is not None


def test_unsettled_preemption_recovers_once_after_a_crash(
    isolated_services: ApiServices,
) -> None:
    """A crash between the terminal commit and the settle call must not strand the task.

    The container terminal state and the preemption intent commit together, so the
    intent survives; recovery settles it exactly once and is a no-op thereafter.
    """
    task, container = _running_task(
        isolated_services,
        kind=StubKind.Function,
        name="preempted-crash-recovery",
    )
    with isolated_services.context.database.session() as session:
        repository = ContainerRepository(session)
        stranded = container.model_copy(
            update={
                "status": ContainerStatus.Failed,
                "exit_code": 562,
                "termination_reason": StopContainerReason.Preempted,
                "finished_at": utc_now(),
                "preemption_settled_at": None,
            }
        )
        repository.records.upsert(
            stranded,
            workspace_id=stranded.workspace_id,
            name=stranded.name,
            status=stranded.status.value,
        )
    service = PreemptedContainerService(
        services=isolated_services,
        stubs=isolated_services.control_plane_service,
    )

    recovered = service.recover_unsettled()
    replayed = service.recover_unsettled()

    assert recovered == [container.id]
    assert isolated_services.tasks.get(task.id).status is TaskStatus.Retry
    assert replayed == []


def test_endpoint_preemption_fails_without_blind_replay(
    isolated_services: ApiServices,
) -> None:
    task, container = _running_task(
        isolated_services,
        kind=StubKind.Endpoint,
        name="preempted-endpoint",
    )
    service = PreemptedContainerService(
        services=isolated_services,
        stubs=isolated_services.control_plane_service,
    )

    result = service.preempted(container, exit_code=562)

    assert result.task_id == task.id
    assert result.status is TaskStatus.Failed
    assert result.changed
    assert not result.retry_scheduled
    assert isolated_services.tasks.get(task.id).status is TaskStatus.Failed
