from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from uuid import uuid4

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService, StubKind
from coordination.event_bus import (
    EventBusEvent,
    EventBusSendResult,
    EventBusSendStatus,
    event_channel_key,
    event_id_for_event,
    event_key,
)
from database.repositories.billing_ledger import ContainerBillingShapeRepository
from database.repositories.identity import WorkspaceMemberRepository, WorkspaceRepository
from database.repositories.orchestration import (
    ContainerRepository,
    MachineRepository,
    WorkerRepository,
)
from execution.containers.planning import ContainerSchedulingOptions
from execution.containers.runtime_state import RedisContainerRuntimeStateRepository
from execution.containers.scheduling import ContainerSchedulingPersistenceService
from scheduler.containers import (
    SchedulerContainerCancellationResult,
    SchedulerContainerSubmitResult,
    SchedulerContainerSubmitStatus,
)
from scheduler.state import SchedulerWorkerRequest
from shared.billing_quotes import ContainerShape
from shared.compute_fleet import Machine, Worker
from shared.container_requests import StopContainerReason, WorkerStartupKind
from shared.containers import ContainerRecord
from shared.errors import ConflictError, InvalidInputError, NotFoundError
from shared.tasks import TaskStatus
from shared.usage import UsageBillingOwner
from shared.workload_keys import (
    pod_container_connections_key,
    pod_keep_warm_lock_key,
    pod_total_connections_key,
)
from tests.real_redis import RealRedisActors
from tests.service_fixtures import workspace_owner_user_id


class _Scheduler:
    def __init__(self) -> None:
        self.requests: list[SchedulerWorkerRequest] = []

    def submit(
        self,
        request: SchedulerWorkerRequest,
        *,
        ready_at: datetime | None = None,
    ) -> SchedulerContainerSubmitResult:
        del ready_at
        self.requests.append(request)
        return SchedulerContainerSubmitResult(
            status=SchedulerContainerSubmitStatus.Queued,
            container_id=request.container_id,
        )


class _Cancellation:
    def __init__(self, result: SchedulerContainerCancellationResult) -> None:
        self.result = result
        self.container_ids: list[str] = []

    def cancel(self, container_id: str) -> SchedulerContainerCancellationResult:
        self.container_ids.append(container_id)
        return self.result.model_copy(update={"container_id": container_id})


class _FailingCancellation:
    def cancel(self, container_id: str) -> SchedulerContainerCancellationResult:
        del container_id
        raise RuntimeError("scheduler cancellation failed")


class _EventBus:
    def __init__(self) -> None:
        self.events: list[EventBusEvent] = []

    def send(self, event: EventBusEvent) -> EventBusSendResult:
        self.events.append(event)
        event_id = event_id_for_event(event)
        return EventBusSendResult(
            status=EventBusSendStatus.Sent,
            event_id=event_id,
            event_key=event_key(event_id),
            channel=event_channel_key(event.type),
            published=1,
        )


class _FailingEventBus:
    def send(self, event: EventBusEvent) -> EventBusSendResult:
        del event
        raise RuntimeError("event delivery failed")


def test_runtime_assignment_separates_operational_and_compute_ownership(
    isolated_services: ApiServices,
) -> None:
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
        sibling_workspace = WorkspaceRepository(session).create(name=f"sibling-{uuid4()}")
        other_workspace = WorkspaceRepository(session).create(name=f"other-{uuid4()}")
        machine = MachineRepository(session).upsert(
            Machine(id=str(uuid4())),
            workspace_id=workspace_id,
        )
        worker = WorkerRepository(session).upsert(
            Worker(id=str(uuid4()), machine_id=machine.id),
            workspace_id=workspace_id,
        )
        foreign_machine = MachineRepository(session).upsert(
            Machine(id=str(uuid4())),
            workspace_id=other_workspace.id,
        )
        foreign_worker = WorkerRepository(session).upsert(
            Worker(id=str(uuid4()), machine_id=foreign_machine.id),
            workspace_id=other_workspace.id,
        )
        container = ContainerRepository(session).upsert(
            ContainerRecord(
                id=str(uuid4()),
                name="runtime-assignment",
                image="",
                command=[],
                workspace_id=workspace_id,
            )
        )
    # The machine's own workspace and the container's differ on purpose: a joined
    # machine belongs to an account, so the account is what the assignment compares.
    owner_user_id = workspace_owner_user_id(isolated_services.context, workspace_id)
    with isolated_services.context.database.session() as session:
        WorkspaceMemberRepository(session).ensure_owner(
            workspace_id=sibling_workspace.id,
            user_id=owner_user_id,
        )
    workspace_owner_user_id(isolated_services.context, other_workspace.id)
    with isolated_services.context.database.session() as session:
        sibling_container = ContainerRepository(session).upsert(
            ContainerRecord(
                id=str(uuid4()),
                name="sibling-runtime-assignment",
                image="",
                command=[],
                workspace_id=sibling_workspace.id,
            )
        )

    persistence = ContainerSchedulingPersistenceService(
        isolated_services.context,
        isolated_services.events,
        isolated_services.workspace_changes,
    )
    persistence.assign_runtime(
        container_id=container.id,
        workspace_id=workspace_id,
        runtime_worker_id="compose-worker",
        runtime_machine_id="compose-machine",
    )
    managed = isolated_services.containers.get(container.id)
    assert managed.runtime_worker_id == "compose-worker"
    assert managed.runtime_machine_id == "compose-machine"
    assert managed.worker_id is None
    assert managed.machine_id is None

    persistence.assign_runtime(
        container_id=container.id,
        workspace_id=workspace_id,
        runtime_worker_id=worker.id,
        runtime_machine_id=machine.id,
        compute_worker_id=worker.id,
        compute_machine_id=machine.id,
    )
    private = isolated_services.containers.get(container.id)
    assert private.worker_id == worker.id
    assert private.machine_id == machine.id

    # The same account's other workspace places on the same machine: that is what
    # connecting the hardware bought, and it is the whole of the ownership rule.
    persistence.assign_runtime(
        container_id=sibling_container.id,
        workspace_id=sibling_workspace.id,
        runtime_worker_id=worker.id,
        runtime_machine_id=machine.id,
        compute_worker_id=worker.id,
        compute_machine_id=machine.id,
    )
    sibling = isolated_services.containers.get(sibling_container.id)
    assert sibling.worker_id == worker.id
    assert sibling.machine_id == machine.id

    with pytest.raises(ConflictError, match="assignment's account"):
        persistence.assign_runtime(
            container_id=container.id,
            workspace_id=workspace_id,
            runtime_worker_id=foreign_worker.id,
            runtime_machine_id=foreign_machine.id,
            compute_worker_id=foreign_worker.id,
            compute_machine_id=foreign_machine.id,
        )

    persistence.clear_runtime_assignment(
        container_id=container.id,
        runtime_worker_id=worker.id,
    )
    cleared = isolated_services.containers.get(container.id)
    assert cleared.runtime_worker_id == ""
    assert cleared.runtime_machine_id == ""
    assert cleared.worker_id == worker.id
    assert cleared.machine_id == machine.id


def test_checkpoint_gpu_limit_rejects_before_scheduler_submission(
    isolated_services: ApiServices,
) -> None:
    scheduler = _Scheduler()
    isolated_services = replace(
        isolated_services,
        containers=replace(isolated_services.containers, scheduler=scheduler),
    )
    container = ContainerRecord(
        id="checkpoint-pod",
        name="checkpoint-pod",
        image="image",
        command=["python", "-m", "app"],
        workspace_id="workspace",
        stub_id="stub",
    )

    with pytest.raises(
        InvalidInputError,
        match="checkpointing does not support more than one GPU",
    ):
        isolated_services.containers.submit_scheduler_request(
            container,
            ContainerSchedulingOptions(
                workspace_name="workspace",
                startup_kind=WorkerStartupKind.Pod,
                checkpoint_enabled=True,
                checkpoint_readiness_path="/ready",
                checkpoint_readiness_port=8080,
                gpu_count=2,
                disk_mib=2048,
            ),
        )

    assert scheduler.requests == []


def test_container_stop_targets_only_assigned_worker(
    isolated_services: ApiServices,
) -> None:
    scheduler = _Scheduler()
    cancellation = _Cancellation(
        SchedulerContainerCancellationResult(
            container_id="placeholder",
            state_found=True,
            worker_id="worker-1",
            worker_stop_required=True,
        )
    )
    events = _EventBus()
    isolated_services = replace(
        isolated_services,
        containers=replace(
            isolated_services.containers,
            scheduler=scheduler,
            scheduler_cancellation=cancellation,
            event_bus=events,
        ),
    )
    container = isolated_services.containers.run(
        "assigned-container",
        "python:3.12",
        ["python", "-c", "print('ok')"],
    )

    isolated_services.containers.stop(container.id)

    assert cancellation.container_ids == [container.id]
    assert len(events.events) == 1
    # UNKNOWN, not USER. The worker writes back whatever it is told, so sending
    # the settlement default would put the customer's name on a stop nobody
    # attributed to them.
    assert events.events[0].args == {
        "container_id": container.id,
        "force": False,
        "reason": "UNKNOWN",
        "worker_id": "worker-1",
    }


def test_container_stop_does_not_broadcast_for_unassigned_request(
    isolated_services: ApiServices,
) -> None:
    scheduler = _Scheduler()
    cancellation = _Cancellation(
        SchedulerContainerCancellationResult(
            container_id="placeholder",
            state_found=True,
            pending_request_removed=True,
        )
    )
    events = _EventBus()
    isolated_services = replace(
        isolated_services,
        containers=replace(
            isolated_services.containers,
            scheduler=scheduler,
            scheduler_cancellation=cancellation,
            event_bus=events,
        ),
    )
    container = isolated_services.containers.run(
        "pending-container",
        "python:3.12",
        ["python", "-c", "print('ok')"],
    )

    isolated_services.containers.stop(container.id)

    assert cancellation.container_ids == [container.id]
    assert events.events == []


def test_deleting_a_live_container_is_refused_until_it_is_stopped(
    isolated_services: ApiServices,
) -> None:
    """Deleting the row under a running container is what ends its metering.

    The worker's usage writes are authorized against that row and the ledger
    prices from the placement recorded beside it, so a delete accepted while the
    container runs bills nothing for the rest of the run.
    """

    isolated_services = replace(
        isolated_services,
        containers=replace(
            isolated_services.containers,
            scheduler=_Scheduler(),
            scheduler_cancellation=_Cancellation(
                SchedulerContainerCancellationResult(
                    container_id="placeholder",
                    state_found=True,
                    pending_request_removed=True,
                )
            ),
            event_bus=_EventBus(),
        ),
    )
    container = isolated_services.containers.run(
        "live-container",
        "python:3.12",
        ["python", "-c", "print('ok')"],
    )

    with pytest.raises(ConflictError):
        isolated_services.containers.delete(container.id)
    assert isolated_services.containers.get(container.id).id == container.id

    isolated_services.containers.stop(container.id)
    isolated_services.containers.delete(container.id)

    with pytest.raises(NotFoundError):
        isolated_services.containers.get(container.id)


@pytest.mark.parametrize(
    ("failure_phase", "expected_error"),
    [
        ("cancellation", "scheduler cancellation failed"),
        ("delivery", "event delivery failed"),
    ],
)
def test_container_stop_failure_never_persists_success(
    isolated_services: ApiServices,
    failure_phase: str,
    expected_error: str,
) -> None:
    cancellation: _Cancellation | _FailingCancellation
    event_bus: _EventBus | _FailingEventBus
    if failure_phase == "cancellation":
        cancellation = _FailingCancellation()
        event_bus = _EventBus()
    else:
        cancellation = _Cancellation(
            SchedulerContainerCancellationResult(
                container_id="placeholder",
                state_found=True,
                worker_id="worker-1",
                worker_stop_required=True,
            )
        )
        event_bus = _FailingEventBus()
    isolated_services = replace(
        isolated_services,
        containers=replace(
            isolated_services.containers,
            scheduler=_Scheduler(),
            scheduler_cancellation=cancellation,
            event_bus=event_bus,
        ),
    )
    container = isolated_services.containers.run(
        f"{failure_phase}-failure",
        "python:3.12",
        ["python", "-c", "print('ok')"],
    )

    with pytest.raises(RuntimeError, match=expected_error):
        isolated_services.containers.stop(container.id)

    assert isolated_services.containers.get(container.id).status == container.status
    assert container.task_id is not None
    assert isolated_services.tasks.get(container.task_id).status is TaskStatus.Pending


def test_stopping_a_container_gives_up_the_redis_state_it_held(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    """A terminal container must not keep pinning the deployment.

    Its keep-warm marker carries no TTL when the workload asked never to scale to
    zero, and its share of the connection count is what decides whether the
    deployment may scale down at all. Both used to be released by whichever
    caller remembered, so a container that died any way other than being stopped
    kept both forever — the marker inert, the count billing.
    """
    redis = real_redis_actors.client()
    isolated_services = replace(
        isolated_services,
        containers=replace(
            isolated_services.containers,
            scheduler=_Scheduler(),
            scheduler_cancellation=_Cancellation(
                SchedulerContainerCancellationResult(
                    container_id="placeholder",
                    state_found=False,
                    worker_id="",
                    worker_stop_required=False,
                )
            ),
            event_bus=_EventBus(),
            runtime_state=RedisContainerRuntimeStateRepository(redis),
        ),
    )
    app = isolated_services.apps.create("released_on_stop_app")
    stub = ControlPlaneService(isolated_services.context).create_stub(
        "released_on_stop_pod",
        kind=StubKind.Pod,
        handler="pkg.workloads:handler",
        app_id=app.id,
        config={"image": {"image_id": "image-pod"}},
    )
    container = isolated_services.containers.run(
        "released-on-stop",
        "python:3.12",
        ["python", "-c", "print('ok')"],
        stub_id=stub.id,
    )
    stub_id = container.stub_id or ""
    keep_warm = redis.key(pod_keep_warm_lock_key(container.workspace_id, stub_id, container.id))
    connections = redis.key(
        pod_container_connections_key(container.workspace_id, stub_id, container.id)
    )
    total = redis.key(pod_total_connections_key(container.workspace_id, stub_id))
    redis.set(keep_warm, "1")
    redis.set(connections, 2)
    redis.set(total, 5)

    isolated_services.containers.stop(container.id)

    assert not redis.exists(keep_warm)
    assert not redis.exists(connections)
    # The stub total drops by exactly this container's share, so the containers
    # still serving traffic keep theirs.
    assert int(str(redis.get(total))) == 3


def test_placing_a_container_records_the_shape_it_will_be_priced_on(
    isolated_services: ApiServices,
) -> None:
    """The control plane's own account of what it placed, written where it decides.

    Pricing reads this and nothing else: the worker's own claims about its
    hardware never reach it. Without the write a container prices against
    nothing, which is indistinguishable at every total from a container that cost
    nothing — so the platform bills a full GPU hour as free and says so nowhere.
    """

    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
        container = ContainerRepository(session).upsert(
            ContainerRecord(
                id=str(uuid4()),
                name="priced-placement",
                image="",
                command=[],
                workspace_id=workspace_id,
            )
        )
    persistence = ContainerSchedulingPersistenceService(
        isolated_services.context,
        isolated_services.events,
        isolated_services.workspace_changes,
    )
    placed = ContainerShape(
        billing_owner=UsageBillingOwner.SelfHosted,
        gpu_type="H100",
        cpu_millicores=4_000,
        memory_mib=8_192,
        gpu_count=2,
    )

    persistence.assign_runtime(
        container_id=container.id,
        workspace_id=workspace_id,
        runtime_worker_id="compose-worker",
        runtime_machine_id="compose-machine",
        shape=placed,
    )

    with isolated_services.context.database.session() as session:
        recorded = ContainerBillingShapeRepository(session).shape_for(container.id)
    assert recorded == placed

    # A placement is decided once. A retried assignment restates it rather than
    # repricing a container that is already running.
    persistence.assign_runtime(
        container_id=container.id,
        workspace_id=workspace_id,
        runtime_worker_id="compose-worker",
        runtime_machine_id="compose-machine",
        shape=ContainerShape(
            billing_owner=UsageBillingOwner.PlatformFleet,
            gpu_type="",
            cpu_millicores=1,
            memory_mib=1,
            gpu_count=0,
        ),
    )
    with isolated_services.context.database.session() as session:
        assert ContainerBillingShapeRepository(session).shape_for(container.id) == placed


def test_a_terminal_container_cannot_claim_a_task(
    isolated_services: ApiServices,
) -> None:
    """A start that arrives after the container ended is refused, not written.

    Releasing a claim and the container noticing it should stop are not ordered
    against each other, so a start can arrive from a container the platform has
    already finished settling. Written, that claim names a container every
    settlement path has run past: no claim query can see the row, no retry
    reaches it, and its caller waits for a result nothing is left to produce.
    """

    container = isolated_services.containers.run(
        "retired-container",
        "python:3.12",
        ["python", "-c", "print('ok')"],
    )
    task = isolated_services.tasks.create("released-invocation", container_id=container.id)
    isolated_services.tasks.start(task.id, container_id=container.id)

    # The platform's own stop, which hands the invocation back rather than
    # cancelling it — the state a late start arrives into.
    isolated_services.containers.stop(container.id, reason=StopContainerReason.Scheduler)
    assert isolated_services.tasks.get(task.id).container_id is None

    with pytest.raises(ConflictError):
        isolated_services.tasks.start(task.id, container_id=container.id)

    assert isolated_services.tasks.get(task.id).container_id is None
