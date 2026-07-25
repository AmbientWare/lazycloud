from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService, StubKind, StubRecord
from coordination.redis_client import RedisClient
from database.repositories.execution import QueueRepository
from database.repositories.orchestration import ContainerRepository
from execution.taskqueues.planning import task_queue_task_heartbeat_key
from execution.taskqueues.service import TaskQueueControlService
from pydantic import BaseModel, JsonValue
from runner.invocation import cloudpickle_bytes
from runner.taskqueue import (
    TaskQueueRunner,
    TaskQueueRunnerConfig,
    decode_task_queue_invocation,
)
from shared.bytes_transport import encode_bytes
from shared.containers import ContainerRecord, ContainerStatus
from shared.http.gateway_tasks import AppendTaskLogRequest
from shared.http.taskqueues import (
    TaskQueueCompleteBody,
    TaskQueueInvocationEnvelope,
    TaskQueueMonitorRequest,
    TaskQueuePopRequest,
    TaskQueueSerializedInvocation,
    TaskQueueTaskMessage,
)
from shared.lifecycle import LifecycleHooks, LifecycleStartupContext, LifecycleTaskContext
from shared.queue_messages import QueueMessage
from shared.tasks import TaskStatus
from shared.timestamps import utc_now
from shared.usage import UsageMetric
from shared.usage_query import UsageQuery
from shared.workload_keys import (
    task_queue_keep_warm_lock_key,
    task_queue_processing_lock_key,
    task_queue_running_lock_index_key,
    task_queue_running_lock_key,
)
from tests.redis_fakes import FakeRedis


class RetryableTaskQueueError(Exception):
    pass


class PermanentTaskQueueError(Exception):
    pass


RETRY_ATTEMPTS = 0
TASK_QUEUE_CONTAINER_ID = "00000000-0000-0000-0000-000000000101"
CURRENT_TASK_QUEUE_CONTAINER_ID = "00000000-0000-0000-0000-000000000102"
STALE_TASK_QUEUE_CONTAINER_ID = "00000000-0000-0000-0000-000000000103"


class QueuePayload(BaseModel):
    value: str
    count: int = 10


class QueueResult(BaseModel):
    value: str


def complete_handler(value: str) -> dict[str, JsonValue]:
    print(f"processing {value}", flush=True)
    return {"value": value}


def queue_on_start(context: LifecycleStartupContext) -> None:
    print(f"queue-start:{context.stub_id}")


def queue_on_running(context: LifecycleTaskContext) -> None:
    print(f"queue-running:{context.status}")


def queue_on_success(context: LifecycleTaskContext) -> None:
    print(f"queue-success:{context.result_available}")


def queue_on_finish(context: LifecycleTaskContext) -> None:
    print(f"queue-finish:{context.status}")


def typed_complete_handler(payload: QueuePayload) -> QueueResult:
    return QueueResult(value=f"{payload.value}:{payload.count}")


def retry_then_complete(value: str) -> dict[str, JsonValue]:
    global RETRY_ATTEMPTS
    RETRY_ATTEMPTS += 1
    if RETRY_ATTEMPTS == 1:
        raise RetryableTaskQueueError("retry this task")
    return {"value": value}


def permanent_failure() -> None:
    raise PermanentTaskQueueError("do not retry")


def _invocation_bytes(*args: JsonValue, **kwargs: JsonValue) -> bytes:
    return cloudpickle_bytes(TaskQueueInvocationEnvelope(args=args, kwargs=kwargs))


def test_task_queue_pop_leases_and_complete_acks_after_result_persisted(
    isolated_services: ApiServices,
) -> None:
    service = _task_queue_service(isolated_services)
    stub = _create_stub(isolated_services, complete_handler)
    put = service.task_queue_put(
        stub.id,
        _invocation_bytes("clip.mp4"),
    )

    assert put.task_id
    assert len(_messages(isolated_services, stub)) == 1
    assert _usage_quantity(isolated_services, stub.workspace_id, UsageMetric.TaskCount) == 1
    stored_record = _messages(isolated_services, stub)[0]
    stored_message = TaskQueueTaskMessage.model_validate(stored_record.body)
    assert decode_task_queue_invocation(stored_message.invocation) == TaskQueueInvocationEnvelope(
        args=("clip.mp4",), kwargs={}
    )
    assert isinstance(stored_record.body, dict)
    assert "payload" not in stored_record.body

    pop = service.task_queue_pop(_pop_request(stub.id))

    assert pop.task_msg
    leased = _messages(isolated_services, stub)
    assert len(leased) == 1
    assert leased[0].leased_until is not None
    assert isolated_services.tasks.get(put.task_id).status is TaskStatus.Running
    workspace = ControlPlaneService(isolated_services.context).get_workspace(stub.workspace_id)
    processing_lock_key = service.redis.key(
        task_queue_processing_lock_key(workspace.name, stub.id, TASK_QUEUE_CONTAINER_ID)
    )
    running_lock_index_key = service.redis.key(
        task_queue_running_lock_index_key(workspace.name, stub.id, TASK_QUEUE_CONTAINER_ID)
    )
    running_lock_key = service.redis.key(
        task_queue_running_lock_key(workspace.name, stub.id, TASK_QUEUE_CONTAINER_ID, put.task_id)
    )
    assert service.redis.exists(processing_lock_key)
    assert service.redis.set_members(running_lock_index_key) == {put.task_id}
    assert service.redis.exists(running_lock_key)

    complete = service.task_queue_complete(
        TaskQueueCompleteBody(
            task_id=put.task_id,
            stub_id=stub.id,
            container_id=TASK_QUEUE_CONTAINER_ID,
            task_status=TaskStatus.Complete,
            keep_warm_seconds=30,
            value_base64=encode_bytes(cloudpickle_bytes({"done": True})),
        )
    )

    task = isolated_services.tasks.get(put.task_id)
    assert complete.final_status is TaskStatus.Complete
    assert complete.message == "complete"
    assert _messages(isolated_services, stub) == []
    assert task.status is TaskStatus.Complete
    assert task.result is not None
    assert not service.redis.exists(processing_lock_key)
    assert not service.redis.exists(running_lock_key)
    assert put.task_id not in service.redis.set_members(running_lock_index_key)
    keep_warm_lock_key = service.redis.key(
        task_queue_keep_warm_lock_key(workspace.name, stub.id, TASK_QUEUE_CONTAINER_ID)
    )
    assert service.redis.exists(keep_warm_lock_key)
    assert service.redis.ttl(keep_warm_lock_key) == 30


def test_task_queue_invocation_decoder_rejects_malformed_envelopes() -> None:
    missing_kwargs = TaskQueueSerializedInvocation.from_bytes(cloudpickle_bytes({"args": []}))
    with pytest.raises(ValueError, match="invalid task queue invocation envelope"):
        decode_task_queue_invocation(missing_kwargs)

    extra_field = TaskQueueSerializedInvocation.from_bytes(
        cloudpickle_bytes({"args": [], "kwargs": {}, "unexpected": True})
    )
    with pytest.raises(ValueError, match="invalid task queue invocation envelope"):
        decode_task_queue_invocation(extra_field)

    with pytest.raises(ValueError, match="invalid task queue invocation envelope"):
        decode_task_queue_invocation(TaskQueueSerializedInvocation.from_bytes(b"not-a-pickle"))


def test_task_queue_state_owns_wait_age_and_live_consumer_capacity(
    isolated_services: ApiServices,
) -> None:
    service = _task_queue_service(isolated_services)
    stub = _create_stub(isolated_services, complete_handler, workers=2)
    container_id = "00000000-0000-0000-0000-000000000042"
    put = service.task_queue_put(
        stub.id,
        _invocation_bytes("clip.mp4"),
    )
    with isolated_services.context.database.session() as session:
        ContainerRepository(session).upsert(
            ContainerRecord(
                id=container_id,
                name="taskqueue-consumer",
                image="taskqueue:local",
                command=["python3.12", "-m", "runner.taskqueue"],
                workspace_id=stub.workspace_id,
                stub_id=stub.id,
                status=ContainerStatus.Running,
            )
        )

    waiting = service.task_queue_state(stub.id)

    assert waiting.queue_depth == 1
    assert waiting.oldest_pending_at is not None
    assert waiting.active_consumers == 2
    assert waiting.busy_consumers == 0
    assert waiting.available_consumers == 2

    assert service.task_queue_pop(
        TaskQueuePopRequest(stub_id=stub.id, container_id=container_id)
    ).task_msg
    claimed = service.task_queue_state(stub.id)

    assert claimed.queue_depth == 1
    assert claimed.oldest_pending_at is None
    assert claimed.active_consumers == 2
    assert claimed.busy_consumers == 1
    assert claimed.available_consumers == 1
    assert isolated_services.tasks.get(put.task_id).status is TaskStatus.Running


def test_task_queue_expiration_marks_pending_task_and_acks_message(
    isolated_services: ApiServices,
) -> None:
    service = _task_queue_service(isolated_services)
    stub = _create_stub(
        isolated_services,
        complete_handler,
        task_ttl_seconds=30,
    )
    put = service.task_queue_put(
        stub.id,
        _invocation_bytes("clip.mp4"),
    )
    message = _messages(isolated_services, stub)[0]
    assert message.expires_at is not None

    expired = service.expire_pending_tasks(
        stub.id,
        now=message.expires_at + timedelta(seconds=1),
    )

    task = isolated_services.tasks.get(put.task_id)
    assert expired == 1
    assert task.status is TaskStatus.Expired
    assert task.error == "task queue item expired before execution"
    assert _messages(isolated_services, stub) == []


def test_task_queue_expiration_does_not_interrupt_running_task(
    isolated_services: ApiServices,
) -> None:
    service = _task_queue_service(isolated_services)
    stub = _create_stub(
        isolated_services,
        complete_handler,
        task_ttl_seconds=30,
    )
    put = service.task_queue_put(
        stub.id,
        _invocation_bytes("clip.mp4"),
    )
    assert service.task_queue_pop(_pop_request(stub.id)).task_msg
    message = _messages(isolated_services, stub)[0]
    assert message.expires_at is not None

    expired = service.expire_pending_tasks(
        stub.id,
        now=message.expires_at + timedelta(seconds=1),
    )

    assert expired == 0
    assert isolated_services.tasks.get(put.task_id).status is TaskStatus.Running
    assert len(_messages(isolated_services, stub)) == 1


def test_task_queue_runner_releases_retryable_failures_without_losing_message(
    isolated_services: ApiServices,
) -> None:
    global RETRY_ATTEMPTS
    RETRY_ATTEMPTS = 0
    service = _task_queue_service(isolated_services)
    stub = _create_stub(
        isolated_services,
        retry_then_complete,
        retry_for=[RetryableTaskQueueError],
        retries=2,
    )
    put = service.task_queue_put(
        stub.id,
        _invocation_bytes("clip.mp4"),
    )
    runner = TaskQueueRunner(
        config=_runner_config(stub),
        channel=_TaskQueueServiceChannel(service),
    )

    first = runner.run_once()

    assert first is not None
    assert first.status is TaskStatus.Retry
    task = isolated_services.tasks.get(put.task_id)
    assert task.status is TaskStatus.Retry
    assert "RetryableTaskQueueError" in (task.error or "")
    retry_message = TaskQueueTaskMessage.model_validate(_messages(isolated_services, stub)[0].body)
    assert retry_message.task_id == put.task_id
    attempts = isolated_services.tasks.attempts(put.task_id)
    assert len(attempts) == 1
    assert attempts[0].status is TaskStatus.Retry
    assert _messages(isolated_services, stub)[0].leased_until is None

    second = runner.run_once()

    assert second is not None
    assert second.status is TaskStatus.Complete
    assert isolated_services.tasks.get(put.task_id).status is TaskStatus.Complete
    assert len(isolated_services.tasks.attempts(put.task_id)) == 2
    assert _messages(isolated_services, stub) == []


def test_task_queue_monitor_tracks_cancelled_status_and_acks_claim(
    isolated_services: ApiServices,
) -> None:
    service = _task_queue_service(isolated_services)
    stub = _create_stub(isolated_services, complete_handler)
    put = service.task_queue_put(
        stub.id,
        _invocation_bytes("clip.mp4"),
    )
    assert service.task_queue_pop(_pop_request(stub.id)).task_msg

    isolated_services.tasks.cancel(put.task_id)
    monitor = service.task_queue_monitor(
        TaskQueueMonitorRequest(
            task_id=put.task_id,
            stub_id=stub.id,
            container_id=TASK_QUEUE_CONTAINER_ID,
        )
    )

    assert monitor.cancelled
    assert isolated_services.tasks.get(put.task_id).status is TaskStatus.Cancelled
    assert _messages(isolated_services, stub) == []


def test_task_queue_monitor_refresh_preserves_active_container_ownership(
    isolated_services: ApiServices,
) -> None:
    service = _task_queue_service(isolated_services)
    stub = _create_stub(isolated_services, complete_handler)
    put = service.task_queue_put(stub.id, _invocation_bytes("clip.mp4"))
    assert service.task_queue_pop(_pop_request(stub.id)).task_msg
    workspace = ControlPlaneService(isolated_services.context).get_workspace(stub.workspace_id)
    heartbeat_key = service.redis.key(
        task_queue_task_heartbeat_key(workspace.name, stub.id, put.task_id)
    )

    before = service.redis.get(heartbeat_key)
    monitor = service.task_queue_monitor(
        TaskQueueMonitorRequest(
            task_id=put.task_id,
            stub_id=stub.id,
            container_id=TASK_QUEUE_CONTAINER_ID,
        )
    )

    assert not monitor.cancelled
    assert not monitor.complete
    assert before == TASK_QUEUE_CONTAINER_ID
    assert service.redis.get(heartbeat_key) == TASK_QUEUE_CONTAINER_ID
    assert service.redis.ttl(heartbeat_key) == 60


def test_task_queue_preemption_releases_once_for_existing_retry_policy(
    isolated_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _task_queue_service(isolated_services)
    stub = _create_stub(
        isolated_services,
        complete_handler,
        retries=1,
        retry_delay_seconds=5,
    )
    put = service.task_queue_put(stub.id, _invocation_bytes("clip.mp4"))
    assert service.task_queue_pop(_pop_request(stub.id)).task_msg
    workspace = ControlPlaneService(isolated_services.context).get_workspace(stub.workspace_id)
    release_calls = 0
    original_release = TaskQueueControlService._release_task_message_for_retry

    def count_release(
        current: TaskQueueControlService,
        message: QueueMessage,
        task_message: TaskQueueTaskMessage,
        *,
        workspace_id: str,
        delay_seconds: float,
    ) -> None:
        nonlocal release_calls
        release_calls += 1
        original_release(
            current,
            message,
            task_message,
            workspace_id=workspace_id,
            delay_seconds=delay_seconds,
        )

    monkeypatch.setattr(
        TaskQueueControlService,
        "_release_task_message_for_retry",
        count_release,
    )
    before = utc_now()

    first = service.task_queue_preempted(
        stub_id=stub.id,
        task_id=put.task_id,
        container_id=TASK_QUEUE_CONTAINER_ID,
        exit_code=137,
    )
    duplicate = service.task_queue_preempted(
        stub_id=stub.id,
        task_id=put.task_id,
        container_id=TASK_QUEUE_CONTAINER_ID,
        exit_code=137,
    )

    task = isolated_services.tasks.get(put.task_id)
    message = _messages(isolated_services, stub)[0]
    assert first.changed
    assert first.retry_scheduled
    assert not first.terminal
    assert first.message_released
    assert not first.message_acknowledged
    assert first.locks_cleared
    assert first.status is TaskStatus.Retry
    assert first.attempt_number == 1
    assert first.max_attempts == 2
    assert not duplicate.changed
    assert not duplicate.message_released
    assert not duplicate.message_acknowledged
    assert release_calls == 1
    assert task.status is TaskStatus.Retry
    assert task.exit_code == 137
    assert task.error == "task queue container was preempted"
    assert message.leased_until is None
    assert message.available_at >= before + timedelta(seconds=5)
    _assert_task_claim_cleared(service, workspace.name, stub.id, put.task_id)


def test_task_queue_preemption_acknowledges_non_retryable_attempt_once(
    isolated_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _task_queue_service(isolated_services)
    stub = _create_stub(isolated_services, complete_handler, retries=0)
    put = service.task_queue_put(stub.id, _invocation_bytes("clip.mp4"))
    assert service.task_queue_pop(_pop_request(stub.id)).task_msg
    ack_calls = 0
    original_ack = TaskQueueControlService._ack_message

    def count_ack(
        current: TaskQueueControlService,
        message_id: str,
        *,
        workspace_id: str,
    ) -> None:
        nonlocal ack_calls
        ack_calls += 1
        original_ack(current, message_id, workspace_id=workspace_id)

    monkeypatch.setattr(TaskQueueControlService, "_ack_message", count_ack)

    first = service.task_queue_preempted(
        stub_id=stub.id,
        task_id=put.task_id,
        container_id=TASK_QUEUE_CONTAINER_ID,
        exit_code=137,
    )
    duplicate = service.task_queue_preempted(
        stub_id=stub.id,
        task_id=put.task_id,
        container_id=TASK_QUEUE_CONTAINER_ID,
        exit_code=137,
    )

    assert first.changed
    assert first.terminal
    assert first.message_acknowledged
    assert first.status is TaskStatus.Failed
    assert not duplicate.changed
    assert not duplicate.message_acknowledged
    assert ack_calls == 1
    assert _messages(isolated_services, stub) == []


@pytest.mark.parametrize("authoritative_status", [TaskStatus.Cancelled, TaskStatus.Timeout])
def test_task_queue_preemption_preserves_authoritative_terminal_state(
    isolated_services: ApiServices,
    authoritative_status: TaskStatus,
) -> None:
    service = _task_queue_service(isolated_services)
    stub = _create_stub(isolated_services, complete_handler, retries=1)
    put = service.task_queue_put(stub.id, _invocation_bytes("clip.mp4"))
    assert service.task_queue_pop(_pop_request(stub.id)).task_msg
    isolated_services.tasks.transition(
        isolated_services.tasks.get(put.task_id),
        authoritative_status,
        error=f"authoritative {authoritative_status.value}",
    )

    result = service.task_queue_preempted(
        stub_id=stub.id,
        task_id=put.task_id,
        container_id=TASK_QUEUE_CONTAINER_ID,
        exit_code=137,
    )

    task = isolated_services.tasks.get(put.task_id)
    assert not result.changed
    assert result.terminal
    assert result.message_acknowledged
    assert result.locks_cleared
    assert result.status is authoritative_status
    assert task.status is authoritative_status
    assert task.error == f"authoritative {authoritative_status.value}"
    assert task.exit_code is None
    assert _messages(isolated_services, stub) == []


def test_task_queue_preemption_preserves_and_releases_authoritative_retry(
    isolated_services: ApiServices,
) -> None:
    service = _task_queue_service(isolated_services)
    stub = _create_stub(isolated_services, complete_handler, retries=1)
    put = service.task_queue_put(stub.id, _invocation_bytes("clip.mp4"))
    assert service.task_queue_pop(_pop_request(stub.id)).task_msg
    isolated_services.tasks.transition(
        isolated_services.tasks.get(put.task_id),
        TaskStatus.Retry,
        error="authoritative retry",
        exit_code=42,
    )
    workspace = ControlPlaneService(isolated_services.context).get_workspace(stub.workspace_id)

    result = service.task_queue_preempted(
        stub_id=stub.id,
        task_id=put.task_id,
        container_id=TASK_QUEUE_CONTAINER_ID,
        exit_code=137,
    )

    task = isolated_services.tasks.get(put.task_id)
    assert not result.changed
    assert result.retry_scheduled
    assert result.message_released
    assert not result.message_acknowledged
    assert result.locks_cleared
    assert result.status is TaskStatus.Retry
    assert task.status is TaskStatus.Retry
    assert task.error == "authoritative retry"
    assert task.exit_code == 42
    assert _messages(isolated_services, stub)[0].leased_until is None
    _assert_task_claim_cleared(service, workspace.name, stub.id, put.task_id)


def test_task_queue_preemption_rejects_stale_container_attempt(
    isolated_services: ApiServices,
) -> None:
    service = _task_queue_service(isolated_services)
    stub = _create_stub(isolated_services, complete_handler, retries=1)
    put = service.task_queue_put(stub.id, _invocation_bytes("clip.mp4"))
    assert service.task_queue_pop(
        TaskQueuePopRequest(stub_id=stub.id, container_id=CURRENT_TASK_QUEUE_CONTAINER_ID)
    ).task_msg

    result = service.task_queue_preempted(
        stub_id=stub.id,
        task_id=put.task_id,
        container_id=STALE_TASK_QUEUE_CONTAINER_ID,
        exit_code=137,
    )

    assert not result.changed
    assert result.stale_attempt
    assert not result.message_released
    assert not result.message_acknowledged
    assert not result.locks_cleared
    assert isolated_services.tasks.get(put.task_id).status is TaskStatus.Running
    assert _messages(isolated_services, stub)[0].leased_until is not None


def test_task_queue_preemption_defers_to_newer_database_attempt_owner(
    isolated_services: ApiServices,
) -> None:
    service = _task_queue_service(isolated_services)
    stub = _create_stub(isolated_services, complete_handler, retries=1)
    put = service.task_queue_put(stub.id, _invocation_bytes("clip.mp4"))
    assert service.task_queue_pop(_pop_request(stub.id)).task_msg
    task = isolated_services.tasks.get(put.task_id)
    task.kwargs["container_id"] = CURRENT_TASK_QUEUE_CONTAINER_ID
    isolated_services.tasks.save(task)

    result = service.task_queue_preempted(
        stub_id=stub.id,
        task_id=put.task_id,
        container_id=TASK_QUEUE_CONTAINER_ID,
        exit_code=137,
    )

    current = isolated_services.tasks.get(put.task_id)
    assert not result.changed
    assert result.stale_attempt
    assert not result.message_released
    assert not result.message_acknowledged
    assert not result.locks_cleared
    assert current.status is TaskStatus.Running
    assert current.kwargs["container_id"] == CURRENT_TASK_QUEUE_CONTAINER_ID
    assert current.exit_code is None
    assert _messages(isolated_services, stub)[0].leased_until is not None


def _create_stub(
    runtime: ApiServices,
    handler: Callable[..., JsonValue | BaseModel | None],
    *,
    retry_for: list[type[BaseException]] | None = None,
    retries: int = 1,
    retry_delay_seconds: float = 0,
    task_ttl_seconds: int | None = None,
    workers: int = 1,
) -> StubRecord:
    retry_refs: list[JsonValue] = [_reference(exception_type) for exception_type in retry_for or []]
    retry_policy: dict[str, JsonValue] = {
        "retry_for": retry_refs,
        "max_attempts": retries + 1,
        "delay_seconds": retry_delay_seconds,
    }
    config: dict[str, JsonValue] = {
        "retry_policy": retry_policy,
        "runtime": {"concurrency": workers},
    }
    if task_ttl_seconds is not None:
        config["task_policy"] = {"ttl": task_ttl_seconds}
    return ControlPlaneService(runtime.context).create_stub(
        f"queue-{handler.__name__}",
        kind=StubKind.TaskQueue,
        handler=_reference(handler),
        config=config,
    )


def _messages(services: ApiServices, stub: StubRecord) -> list[QueueMessage]:
    with services.context.database.session() as session:
        messages = QueueRepository(session).messages.list(workspace_id=stub.workspace_id)
    return [message for message in messages if message.queue == f"taskqueue:{stub.id}"]


def _usage_quantity(services: ApiServices, workspace_id: str, metric: UsageMetric) -> float:
    summary = services.usage.aggregate(query=UsageQuery(workspace_id=workspace_id))
    return {row.metric: row.quantity for row in summary}.get(metric, 0)


def _pop_request(stub_id: str) -> TaskQueuePopRequest:
    return TaskQueuePopRequest(stub_id=stub_id, container_id=TASK_QUEUE_CONTAINER_ID)


def _task_queue_service(services: ApiServices) -> TaskQueueControlService:
    return TaskQueueControlService(services, redis=RedisClient(FakeRedis(), key_prefix="test"))


def _assert_task_claim_cleared(
    service: TaskQueueControlService,
    workspace_name: str,
    stub_id: str,
    task_id: str,
) -> None:
    processing_key = service.redis.key(
        task_queue_processing_lock_key(workspace_name, stub_id, TASK_QUEUE_CONTAINER_ID)
    )
    index_key = service.redis.key(
        task_queue_running_lock_index_key(workspace_name, stub_id, TASK_QUEUE_CONTAINER_ID)
    )
    running_key = service.redis.key(
        task_queue_running_lock_key(workspace_name, stub_id, TASK_QUEUE_CONTAINER_ID, task_id)
    )
    heartbeat_key = service.redis.key(
        task_queue_task_heartbeat_key(workspace_name, stub_id, task_id)
    )
    assert not service.redis.exists(processing_key)
    assert task_id not in service.redis.set_members(index_key)
    assert not service.redis.exists(running_key)
    assert not service.redis.exists(heartbeat_key)


def _runner_config(
    stub: StubRecord,
    *,
    lifecycle_hooks: LifecycleHooks | None = None,
) -> TaskQueueRunnerConfig:
    handler_ref = stub.handler
    if handler_ref is None:
        raise AssertionError(f"task queue stub has no handler: {stub.id}")
    retry_for_refs = stub.config.retry_policy.retry_for if stub.config.retry_policy else ()
    return TaskQueueRunnerConfig(
        stub_id=stub.id,
        handler_ref=handler_ref,
        retry_for_refs=retry_for_refs,
        container_id=TASK_QUEUE_CONTAINER_ID,
        container_hostname="test-host",
        lifecycle_hooks=lifecycle_hooks or LifecycleHooks(),
    )


class _TaskQueueServiceChannel:
    def __init__(self, service: TaskQueueControlService) -> None:
        self.service = service
        self.logs: list[AppendTaskLogRequest] = []

    def post(self, path: str, payload: dict[str, JsonValue] | None = None) -> JsonValue:
        if path == "/api/v1/taskqueues/pop":
            return self.service.task_queue_pop(
                TaskQueuePopRequest.model_validate(payload)
            ).model_dump(mode="json")
        if path == "/api/v1/taskqueues/monitor":
            return self.service.task_queue_monitor(
                TaskQueueMonitorRequest.model_validate(payload)
            ).model_dump(mode="json")
        if path == "/api/v1/taskqueues/complete":
            return self.service.task_queue_complete(
                TaskQueueCompleteBody.model_validate(payload)
            ).model_dump(mode="json")
        if path == "/gateway/tasks/log":
            self.logs.append(AppendTaskLogRequest.model_validate(payload))
            return {"ok": True, "error_msg": ""}
        raise AssertionError(f"unexpected path: {path}")


class _TransientPopFailureChannel(_TaskQueueServiceChannel):
    def __init__(self, service: TaskQueueControlService) -> None:
        super().__init__(service)
        self.failures = 1

    def post(self, path: str, payload: dict[str, JsonValue] | None = None) -> JsonValue:
        if path == "/api/v1/taskqueues/pop" and self.failures:
            self.failures -= 1
            raise OSError("temporary control-plane lookup failure")
        return super().post(path, payload)


def _reference(
    value: Callable[..., JsonValue | BaseModel | None] | type[BaseException],
) -> str:
    return f"{value.__module__}:{value.__qualname__}"
