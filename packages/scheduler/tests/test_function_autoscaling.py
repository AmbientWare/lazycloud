"""What the function autoscaler owes its operator, on the same terms as the others.

A function scales on a different signal from an endpoint or a pod, and that is
the only thing about it that should differ. Pausing it has to stop it, its
decisions have to reach the history an operator reads, and a tick has to leave
the metrics and the state row every other autoscaler leaves — otherwise the
control surface answers for two kinds of workload and silently omits the third.
"""

from __future__ import annotations

from dataclasses import replace

from api.server.services import ApiServices
from control.service import ControlPlaneService, StubConfigUpdateValue, StubKind, StubRecord
from coordination.redis_client import RedisClient
from database.repositories.orchestration import AutoscalerStateRepository
from execution.functions.service import FunctionControlService
from pydantic import JsonValue
from scheduler.autoscaling import AutoscalingDriver, FunctionAutoscaler
from scheduler.containers import (
    SchedulerContainerSubmitResult,
    SchedulerContainerSubmitStatus,
)
from scheduler.state import SchedulerWorkerRequest
from shared.autoscaler_state import AutoscalerTargetKind
from shared.deployment_records import DeploymentSpec, Resources
from shared.deployments import DeploymentKind
from shared.function_payloads import FunctionJsonInvocation
from shared.http.functions import FunctionInvokeBody
from tests.metric_helpers import metric_value
from tests.redis_fakes import FakeRedis


class _Scheduler:
    def __init__(self) -> None:
        self.requests: list[SchedulerWorkerRequest] = []

    def submit(
        self,
        request: SchedulerWorkerRequest,
        *,
        ready_at: object = None,
    ) -> SchedulerContainerSubmitResult:
        del ready_at
        self.requests.append(request)
        return SchedulerContainerSubmitResult(
            status=SchedulerContainerSubmitStatus.Queued,
            container_id=request.container_id,
        )


def test_paused_function_autoscaler_leaves_its_backlog_alone(
    isolated_services: ApiServices,
) -> None:
    """Pause stops the autoscaler that provisions a function, or it stops nothing.

    An operator pauses an autoscaler to stop capacity moving while they look at
    something. The flag is one flag over all three kinds, so a function that
    keeps scaling through it makes the whole control read as advisory.
    """

    scheduler = _Scheduler()
    services = replace(
        isolated_services,
        containers=replace(isolated_services.containers, scheduler=scheduler),
    )
    redis = RedisClient(FakeRedis(), key_prefix="test")
    stub = _create_function_stub(services, max_containers=3)
    _enqueue_invocations(services, stub, count=3)
    # The first container is the invocation's to start, not the autoscaler's, so
    # what pause has to stop is every container after this one.
    started_before_pause = len(scheduler.requests)
    services.autoscaler_operations_service.pause(stub.id, workspace=stub.workspace_id)

    results = _function_autoscaler(services, redis).reconcile()

    # A paused stub is not held, it is not looked at: the same thing the
    # endpoint and pod autoscalers have always done with one.
    assert results == []
    assert len(scheduler.requests) == started_before_pause


def test_function_autoscaler_records_what_it_decided(
    isolated_services: ApiServices,
) -> None:
    """A function's tick leaves the same evidence an endpoint's or a pod's does.

    The state row, the metrics and the history are one operator surface. A kind
    missing from any of them is not a quieter autoscaler, it is one nobody can
    answer questions about.
    """

    scheduler = _Scheduler()
    services = replace(
        isolated_services,
        containers=replace(isolated_services.containers, scheduler=scheduler),
    )
    redis = RedisClient(FakeRedis(), key_prefix="test")
    stub = _create_function_stub(services, max_containers=3)
    _enqueue_invocations(services, stub, count=3)

    results = _function_autoscaler(services, redis).reconcile()

    assert len(results) == 1
    result = results[0]
    # One container is already up: an invocation of an idle stub may start the
    # first, and the autoscaler owns every one after it.
    assert result.current_containers == 1
    assert result.desired_containers == 3
    assert [action.action for action in result.actions] == ["scale-up", "scale-up"]

    metric_labels = {
        "source": "function.autoscaler",
        "workspace_id": stub.workspace_id,
        "stub_id": stub.id,
        "kind": StubKind.Function.value,
    }
    snapshot = services.metrics.latest()
    assert (
        metric_value(
            snapshot.counters,
            "autoscaler_decisions_total",
            **metric_labels,
            decision="scale-up",
        )
        == 1
    )
    assert metric_value(snapshot.gauges, "autoscaler_desired_containers", **metric_labels) == 3
    assert (
        metric_value(
            snapshot.gauges,
            "autoscaler_signal",
            **metric_labels,
            signal="unclaimed_tasks",
        )
        == 3
    )

    with services.context.database.session() as session:
        state = AutoscalerStateRepository(session).get(
            workspace_id=stub.workspace_id,
            target_kind=AutoscalerTargetKind.Function,
            target_id=stub.id,
        )
    assert state is not None
    assert state.signal_name == "unclaimed_tasks"
    assert state.signal_value == 3
    assert state.decision == "scale-up"
    assert len(state.last_actions) == 2

    history = services.autoscaler_operations_service.history(
        workspace=stub.workspace_id,
        target_id=stub.id,
    )
    assert [event.action for event in history.events] == ["function.autoscaler.scale_decision"]


def _create_function_stub(runtime: ApiServices, *, max_containers: int) -> StubRecord:
    deployment = runtime.deployments.deploy(
        DeploymentSpec(
            name="function-autoscale",
            kind=DeploymentKind.Function,
            handler="pkg.function:handler",
            resources=Resources(timeout_seconds=30, concurrency=1),
        )
    )
    control = ControlPlaneService(runtime.context)
    stub = next(
        item
        for item in control.list_stubs()
        if item.deployment_id == deployment.id and item.kind is StubKind.Function
    )
    autoscaler: dict[str, JsonValue] = {
        "max_containers": max_containers,
        "tasks_per_container": 1,
    }
    fields: dict[str, StubConfigUpdateValue] = {
        "image": {"image_id": "img-function"},
        "runtime": {"timeout_seconds": 30, "concurrency": 1},
        "autoscaler": autoscaler,
    }
    return control.update_stub_config(stub.id, fields=fields).stub


def _enqueue_invocations(runtime: ApiServices, stub: StubRecord, *, count: int) -> None:
    """Leave a backlog the autoscaler has to answer for.

    Invoked through the function service rather than written as task rows: the
    first call may start a container of its own, and a backlog the autoscaler
    reads has to be the one invocations actually leave behind.
    """

    functions = FunctionControlService(runtime)
    for value in range(count):
        functions.function_invoke(
            FunctionInvokeBody(
                stub_id=stub.id,
                invocation=FunctionJsonInvocation(args=[value]),
            )
        )


def _function_autoscaler(services: ApiServices, redis: RedisClient) -> AutoscalingDriver:
    return AutoscalingDriver(
        services,
        redis=redis,
        workload=FunctionAutoscaler(services, functions=FunctionControlService(services)),
    )
