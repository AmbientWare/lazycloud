from __future__ import annotations

import json
import socket
from collections.abc import Mapping
from dataclasses import dataclass

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService, StubKind
from execution.callbacks import (
    CallbackDeliveryError,
    HttpTaskCallbackSender,
    TaskCallbackService,
)
from identity.signatures import PayloadSignature, verify_payload_signature
from pydantic import ValidationError
from shared.http.callbacks import TaskCallbackBody
from shared.tasks import RetryPolicy, TaskStatus
from shared.workload_config import StubConfig


@dataclass(frozen=True, slots=True)
class _CallbackCall:
    target: str
    body: bytes
    headers: dict[str, str]


class _CallbackSender:
    def __init__(self, outcomes: list[int | CallbackDeliveryError]) -> None:
        self.outcomes = outcomes
        self.calls: list[_CallbackCall] = []

    def send(self, target: str, body: bytes, headers: Mapping[str, str]) -> int:
        self.calls.append(_CallbackCall(target, body, dict(headers)))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, CallbackDeliveryError):
            raise outcome
        return outcome


def test_terminal_tasks_deliver_signed_callback_for_supported_workloads(
    isolated_services: ApiServices,
) -> None:
    sender = _CallbackSender([204])
    callback_service = TaskCallbackService(
        isolated_services.context,
        isolated_services.events,
        sender=sender,
        now=lambda: 1_720_000_000.0,
    )
    isolated_services.tasks.callback_dispatcher = callback_service
    control_plane = ControlPlaneService(isolated_services.context)
    stub = control_plane.create_stub(
        "callback-function",
        kind=StubKind.Function,
        config={"callback_url": "https://callbacks.example.com/task?source=test"},
    )
    task = isolated_services.tasks.create(
        "callback-task",
        workspace_id=stub.workspace_id,
        stub_id=stub.id,
    )
    running = isolated_services.tasks.transition(task, TaskStatus.Running)

    completed = isolated_services.tasks.transition(
        running,
        TaskStatus.Complete,
        result={"value": 42},
    )

    assert completed.status is TaskStatus.Complete
    assert len(sender.calls) == 1
    call = sender.calls[0]
    callback = TaskCallbackBody.model_validate_json(call.body)
    assert callback.task_id == task.id
    assert callback.root_task_id == task.id
    assert callback.status is TaskStatus.Complete
    assert callback.attempt_number == 1
    assert callback.max_attempts == 1
    assert callback.retry_scheduled is False
    assert callback.data == {"value": 42}
    assert call.target == "https://callbacks.example.com/task?source=test"
    assert call.headers["X-Task-ID"] == task.id
    assert call.headers["X-Task-Status"] == TaskStatus.Complete.value
    assert call.headers["X-Task-Timestamp"] == "1720000000"
    assert len(call.headers["Idempotency-Key"]) == 64
    signature = PayloadSignature(
        key=call.headers["X-Task-Signature"],
        timestamp=int(call.headers["X-Task-Timestamp"]),
    )
    assert verify_payload_signature(
        call.body,
        control_plane.workspace_signing_key(stub.workspace_id),
        signature,
    )
    events = isolated_services.events.list_for_resource(
        resource_type="task",
        resource_id=task.id,
    )
    delivered = next(event for event in events if event.action == "task.callback.delivered")
    assert delivered.data["callback_host"] == "callbacks.example.com"
    assert delivered.data["attempts"] == 1


def test_retry_callback_uses_bounded_delivery_retries_and_stable_idempotency(
    isolated_services: ApiServices,
) -> None:
    sender = _CallbackSender(
        [
            CallbackDeliveryError("callback returned HTTP 503", retryable=True),
            CallbackDeliveryError("callback returned HTTP 429", retryable=True),
            202,
        ]
    )
    delays: list[float] = []
    isolated_services.tasks.callback_dispatcher = TaskCallbackService(
        isolated_services.context,
        isolated_services.events,
        sender=sender,
        sleep=delays.append,
        now=lambda: 1_720_000_000.0,
    )
    stub = ControlPlaneService(isolated_services.context).create_stub(
        "retry-callback",
        kind=StubKind.TaskQueue,
        config={"callback_url": "https://callbacks.example.com/task"},
    )
    task = isolated_services.tasks.create(
        "retry-callback-task",
        workspace_id=stub.workspace_id,
        stub_id=stub.id,
        retry_policy=RetryPolicy(max_attempts=2),
    )
    running = isolated_services.tasks.transition(task, TaskStatus.Running)

    outcome = isolated_services.tasks.finish_with_retry(
        running.id,
        TaskStatus.Failed,
        error="temporary failure",
    )

    assert outcome.task.status is TaskStatus.Retry
    assert len(sender.calls) == 3
    assert delays == [0.25, 0.75]
    assert len({call.body for call in sender.calls}) == 1
    assert len({call.headers["Idempotency-Key"] for call in sender.calls}) == 1
    callback = TaskCallbackBody.model_validate_json(sender.calls[0].body)
    assert callback.status is TaskStatus.Retry
    assert callback.retry_scheduled is True


def test_permanent_callback_failure_is_observable_without_exposing_target_query(
    isolated_services: ApiServices,
) -> None:
    sender = _CallbackSender([CallbackDeliveryError("callback returned HTTP 401", retryable=False)])
    isolated_services.tasks.callback_dispatcher = TaskCallbackService(
        isolated_services.context,
        isolated_services.events,
        sender=sender,
    )
    stub = ControlPlaneService(isolated_services.context).create_stub(
        "failed-callback",
        kind=StubKind.Function,
        config={"callback_url": "https://callbacks.example.com/task?token=secret-value"},
    )
    task = isolated_services.tasks.create(
        "failed-callback-task",
        workspace_id=stub.workspace_id,
        stub_id=stub.id,
    )
    running = isolated_services.tasks.transition(task, TaskStatus.Running)

    completed = isolated_services.tasks.transition(running, TaskStatus.Complete)

    assert completed.status is TaskStatus.Complete
    assert len(sender.calls) == 1
    events = isolated_services.events.list_for_resource(
        resource_type="task",
        resource_id=task.id,
    )
    failed = next(event for event in events if event.action == "task.callback.failed")
    assert failed.data["attempts"] == 1
    assert failed.data["callback_host"] == "callbacks.example.com"
    assert "secret-value" not in json.dumps(failed.data)


@pytest.mark.parametrize(
    "target",
    [
        "file:///tmp/callback",
        "https://user:password@example.com/task",
        "https://example.com/task#fragment",
        "https://example.com:invalid/task",
    ],
)
def test_callback_target_rejects_unsafe_url_shapes(target: str) -> None:
    with pytest.raises(ValidationError):
        StubConfig(callback_url=target)


def test_callback_sender_rejects_hosts_with_private_dns_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def private_dns_answer(
        host: str,
        port: int,
        *,
        type: socket.SocketKind,
    ) -> list[
        tuple[
            socket.AddressFamily,
            socket.SocketKind,
            int,
            str,
            tuple[str, int],
        ]
    ]:
        return [(socket.AF_INET, type, socket.IPPROTO_TCP, host, ("10.0.0.8", port))]

    monkeypatch.setattr("socket.getaddrinfo", private_dns_answer)

    with pytest.raises(CallbackDeliveryError, match="non-public") as raised:
        HttpTaskCallbackSender().send("http://callback.example.com/task", b"{}", {})

    assert raised.value.retryable is False
