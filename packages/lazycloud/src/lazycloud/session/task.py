from __future__ import annotations

import asyncio
import concurrent.futures
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Generic, Protocol, TypeVar, cast

import shared.tasks
from pydantic import JsonValue
from shared.function_payloads import FunctionResultPayload
from shared.http.errors import HttpApiError, HttpResponseDecodeError
from shared.http.observability import LogQueryRequest, LogQueryResponse, LogRecord
from shared.http.tasks import (
    TaskDetailResponse,
    TaskPageResponse,
    TaskResponse,
    TaskStopResponse,
)
from shared.http_transport import HttpChannel
from shared.tasks import TaskStatus, is_terminal_task_status
from shared.transport_retry import (
    TRANSIENT_TRANSPORT_ERRORS,
    TransientRetry,
    call_with_transient_retry,
    is_transient_transport_error,
)

from lazycloud.clients.observability.control import ObservabilityClient
from lazycloud.control import ControlClientConfigMixin
from lazycloud.control_clients import (
    control_http_channel,
    observability_control_client,
    resource_control_client,
)
from lazycloud.function_results import (
    FunctionResultDecodeError,
    decode_function_result,
)
from lazycloud.json_contracts import parse_json_value, validate_json_object

R = TypeVar("R")


class TaskControlClient(Protocol):
    def list_tasks(
        self,
        *,
        stub_ids: tuple[str, ...] = (),
        status: TaskStatus | None = None,
        deployment_id: str | None = None,
        app_id: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> TaskPageResponse: ...

    def task(self, task_id: str) -> TaskDetailResponse: ...

    def stop_tasks(self, task_ids: tuple[str, ...]) -> TaskStopResponse: ...


class TaskOperationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TaskResult:
    task: shared.tasks.Task

    @property
    def id(self) -> str:
        return self.task.id

    @property
    def status(self) -> TaskStatus:
        return self.task.status

    @property
    def ok(self) -> bool:
        return self.task.status is TaskStatus.Complete and (self.task.exit_code in {None, 0})

    @property
    def value(self) -> Any:
        if self.task.function_result is not None:
            return self.task.function_result
        return self.task.result

    @property
    def error(self) -> str:
        return self.task.error or ""

    @property
    def exit_code(self) -> int | None:
        return self.task.exit_code


@dataclass(frozen=True, slots=True)
class TaskLifecycleEvent:
    event: str
    data: dict[str, JsonValue]
    task: shared.tasks.Task | None = None


@dataclass(frozen=True, slots=True)
class TaskSubscription:
    task_id: str
    events: tuple[TaskLifecycleEvent, ...]

    def __iter__(self) -> Iterator[TaskLifecycleEvent]:
        return iter(self.events)

    @property
    def latest(self) -> TaskLifecycleEvent | None:
        return self.events[-1] if self.events else None


class TaskHandleClient(Protocol):
    def get(self, task_id: str) -> TaskDetailResponse: ...

    def get_result_task(self, task_id: str) -> shared.tasks.Task: ...

    def logs(
        self,
        task_id: str,
        *,
        workspace: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> list[LogRecord]: ...

    def subscribe(self, task_id: str) -> TaskSubscription: ...

    def cancel(self, task_id: str) -> TaskStopResponse: ...


@dataclass(slots=True)
class Task:
    task_id: str
    client: TaskHandleClient

    def get(self) -> shared.tasks.Task:
        return self.client.get_result_task(self.task_id)

    def view(self) -> TaskDetailResponse:
        return self.client.get(self.task_id)

    def result(
        self,
        *,
        wait: bool = False,
        timeout_seconds: float | None = None,
        poll_interval_seconds: float = 1.0,
    ) -> TaskResult:
        if wait:
            return self.wait(
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
            )
        return TaskResult(self.get())

    def wait(
        self,
        *,
        timeout_seconds: float | None = None,
        poll_interval_seconds: float = 1.0,
    ) -> TaskResult:
        deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
        retry = TransientRetry(deadline=deadline)
        while True:
            try:
                task = self.get()
            except TRANSIENT_TRANSPORT_ERRORS as exc:
                if not is_transient_transport_error(exc):
                    raise
                try:
                    retry.backoff(exc)
                except TRANSIENT_TRANSPORT_ERRORS:
                    if _task_wait_deadline_exceeded(deadline):
                        msg = (
                            f"task {self.task_id} did not complete within {timeout_seconds} seconds"
                        )
                    else:
                        msg = f"task {self.task_id} status could not be read: {exc}"
                    raise TaskOperationError(msg) from exc
                continue

            retry.reset()
            if is_terminal_task_status(task.status):
                return TaskResult(task)
            if deadline is not None and time.monotonic() >= deadline:
                msg = f"task {self.task_id} did not complete within {timeout_seconds} seconds"
                raise TaskOperationError(msg)
            time.sleep(poll_interval_seconds)

    async def async_wait(
        self,
        *,
        timeout_seconds: float | None = None,
        poll_interval_seconds: float = 1.0,
    ) -> TaskResult:
        return await asyncio.to_thread(
            self.wait,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )

    def logs(self, *, limit: int = 100, cursor: str | None = None) -> list[LogRecord]:
        return self.client.logs(self.task_id, limit=limit, cursor=cursor)

    def output(self, *, limit: int = 100, cursor: str | None = None) -> str:
        return "\n".join(entry.message for entry in self.logs(limit=limit, cursor=cursor))

    def subscribe(self) -> TaskSubscription:
        return self.client.subscribe(self.task_id)

    def cancel(self) -> TaskStopResponse:
        return self.client.cancel(self.task_id)


class FunctionCallClient(Protocol):
    def handle(self, task_id: str) -> Task: ...

    def rerun(self, task_id: str) -> Task: ...


class FunctionCallHandle(Protocol):
    def get(
        self,
        *,
        timeout_seconds: float | None = None,
        poll_interval_seconds: float = 1.0,
    ) -> object: ...


@dataclass(slots=True)
class FunctionCall(Generic[R]):
    task_id: str
    client: FunctionCallClient
    result_payload: FunctionResultPayload | None = None
    complete: bool = False
    exit_code: int = 0
    error: str = ""
    workspace_id: str = ""
    __orig_class__: object = field(init=False, repr=False, compare=False)

    @property
    def task(self) -> Task:
        return self.client.handle(self.task_id)

    def result(
        self,
        *,
        wait: bool = False,
        timeout_seconds: float | None = None,
        poll_interval_seconds: float = 1.0,
    ) -> TaskResult:
        result = self.task.result(
            wait=wait,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        if not result.ok or result.value is None:
            return result
        try:
            decoded = decode_function_result(result.value)
        except FunctionResultDecodeError as exc:
            raise TaskOperationError(f"function task {self.task_id} has an invalid result") from exc
        return TaskResult(
            result.task.model_copy(update={"result": decoded, "function_result": None})
        )

    def get(
        self,
        *,
        timeout_seconds: float | None = None,
        poll_interval_seconds: float = 1.0,
    ) -> R:
        if self.complete:
            if self.exit_code != 0:
                raise TaskOperationError(self.error or f"function task {self.task_id} failed")
            try:
                return cast(R, decode_function_result(self.result_payload))
            except FunctionResultDecodeError as exc:
                raise TaskOperationError(
                    f"function task {self.task_id} has an invalid result"
                ) from exc
        result = self.result(
            wait=True,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        if not result.ok:
            msg = result.error or f"function task {self.task_id} failed"
            raise TaskOperationError(msg)
        return cast(R, result.value)

    def logs(self, *, limit: int = 100, cursor: str | None = None) -> list[LogRecord]:
        return self.task.logs(limit=limit, cursor=cursor)

    def output(self, *, limit: int = 100, cursor: str | None = None) -> str:
        return self.task.output(limit=limit, cursor=cursor)

    def subscribe(self) -> TaskSubscription:
        return self.task.subscribe()

    def cancel(self) -> TaskStopResponse:
        return self.task.cancel()

    def rerun(self) -> FunctionCall[R]:
        task = self.client.rerun(self.task_id)
        return FunctionCall(
            task_id=task.task_id,
            client=self.client,
            workspace_id=self.workspace_id,
        )

    @classmethod
    def gather(
        cls,
        *calls: FunctionCallHandle,
        timeout_seconds: float | None = None,
        poll_interval_seconds: float = 1.0,
        return_exceptions: bool = False,
    ) -> list[Any]:
        results: list[Any] = []
        for call in calls:
            try:
                results.append(
                    call.get(
                        timeout_seconds=timeout_seconds,
                        poll_interval_seconds=poll_interval_seconds,
                    )
                )
            except Exception as exc:
                if not return_exceptions:
                    raise
                results.append(exc)
        return results


@dataclass(frozen=True, slots=True)
class TaskBatch:
    handles: tuple[Task, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "handles", tuple(self.handles))

    def __iter__(self) -> Iterator[Task]:
        return iter(self.handles)

    def __len__(self) -> int:
        return len(self.handles)

    def wait(
        self,
        *,
        timeout_seconds: float | None = None,
        poll_interval_seconds: float = 1.0,
    ) -> list[TaskResult]:
        results: list[TaskResult | None] = [None] * len(self.handles)
        for index, result in self._as_completed_indexed(
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        ):
            results[index] = result
        return [result for result in results if result is not None]

    def as_completed(
        self,
        *,
        timeout_seconds: float | None = None,
        poll_interval_seconds: float = 1.0,
    ) -> Iterator[TaskResult]:
        for _, result in self._as_completed_indexed(
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        ):
            yield result

    def _as_completed_indexed(
        self,
        *,
        timeout_seconds: float | None,
        poll_interval_seconds: float,
    ) -> Iterator[tuple[int, TaskResult]]:
        if not self.handles:
            return
        deadline = _batch_deadline(timeout_seconds)
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=len(self.handles))
        try:
            futures = {
                executor.submit(
                    handle.wait,
                    timeout_seconds=_batch_remaining_seconds(deadline, timeout_seconds),
                    poll_interval_seconds=poll_interval_seconds,
                ): index
                for index, handle in enumerate(self.handles)
            }
            for future in concurrent.futures.as_completed(futures, timeout=timeout_seconds):
                yield futures[future], future.result()
        except concurrent.futures.TimeoutError as exc:
            raise _batch_timeout_error(timeout_seconds) from exc
        finally:
            executor.shutdown(wait=False, cancel_futures=True)


@dataclass(slots=True)
class TaskClient(ControlClientConfigMixin):
    client: TaskControlClient | None = None
    observability_client: ObservabilityClient | None = None
    workspace: str | None = None
    endpoint: str | None = None
    token: str | None = None
    timeout_seconds: float = 10.0

    @property
    def control_client(self) -> TaskControlClient:
        if self.client is None:
            self.client = resource_control_client(self._config())
        return self.client

    @property
    def observability(self) -> ObservabilityClient:
        if self.observability_client is None:
            self.observability_client = observability_control_client(self._config())
        return self.observability_client

    def list(
        self,
        *,
        status: TaskStatus | None = None,
        stub_ids: tuple[str, ...] = (),
        deployment_id: str | None = None,
        app_id: str | None = None,
        limit: int = 100,
    ) -> list[TaskResponse]:
        response = self.control_client.list_tasks(
            stub_ids=stub_ids,
            status=status,
            deployment_id=deployment_id,
            app_id=app_id,
            limit=limit,
        )
        return response.data

    def get(self, task_id: str) -> TaskDetailResponse:
        try:
            return self.control_client.task(task_id)
        except HttpApiError as exc:
            if exc.status_code != 404:
                raise
            raise TaskOperationError(f"task not found: {task_id}") from exc

    def logs(
        self,
        task_id: str,
        *,
        workspace: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> list[LogRecord]:
        response = self.log_query(
            LogQueryRequest(
                workspace_id=workspace or self._config().workspace,
                task_id=task_id,
                limit=limit,
                cursor=cursor,
            )
        )
        return list(response.data)

    def log_query(self, request: LogQueryRequest) -> LogQueryResponse:
        return self.observability.logs(request)

    def cancel(self, task_id: str) -> TaskStopResponse:
        return self.control_client.stop_tasks((task_id,))

    def handle(self, task_id: str) -> Task:
        return Task(task_id=task_id, client=self)

    def task(self, task_id: str) -> Task:
        return self.handle(task_id)

    def get_result_task(self, task_id: str) -> shared.tasks.Task:
        try:
            response = self.detail(task_id)
            return shared.tasks.Task.model_validate(response, from_attributes=True)
        except (TaskOperationError, ValueError) as exc:
            raise TaskOperationError(f"failed to load task result for {task_id}") from exc

    def detail(self, task_id: str) -> TaskDetailResponse:
        return self.control_client.task(task_id)

    def result(self, task_id: str, *, wait: bool = False) -> TaskResult:
        return self.handle(task_id).result(wait=wait)

    def output(self, task_id: str, *, limit: int = 100, cursor: str | None = None) -> str:
        return self.handle(task_id).output(limit=limit, cursor=cursor)

    def subscribe(self, task_id: str) -> TaskSubscription:
        raw = call_with_transient_retry(
            lambda: self._http_channel().get(f"/api/v1/tasks/{task_id}/subscribe")
        )
        return TaskSubscription(task_id=task_id, events=tuple(_parse_task_events(raw)))

    def rerun(self, task_id: str) -> Task:
        raw = self._http_channel().post(f"/api/v1/tasks/{task_id}/rerun")
        try:
            rerun_task = TaskResponse.model_validate(raw)
        except ValueError as exc:
            raise TaskOperationError(f"failed to rerun task {task_id}") from exc
        return self.handle(rerun_task.id)

    def _http_channel(self) -> HttpChannel:
        return control_http_channel(self._config())


def _task_wait_deadline_exceeded(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def _batch_deadline(timeout_seconds: float | None) -> float | None:
    if timeout_seconds is None:
        return None
    return time.monotonic() + timeout_seconds


def _batch_remaining_seconds(
    deadline: float | None,
    timeout_seconds: float | None,
) -> float | None:
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _batch_timeout_error(timeout_seconds)
    return remaining


def _batch_timeout_error(timeout_seconds: float | None) -> TaskOperationError:
    return TaskOperationError(f"task batch did not complete within {timeout_seconds} seconds")


def _parse_task_events(raw: object) -> list[TaskLifecycleEvent]:
    if not isinstance(raw, str):
        try:
            return [_task_event_from_data("message", validate_json_object(raw))]
        except ValueError as exc:
            raise HttpResponseDecodeError("task subscription returned an invalid response") from exc
    events: list[TaskLifecycleEvent] = []
    for block in raw.replace("\r\n", "\n").split("\n\n"):
        event = "message"
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line.removeprefix("event:").strip() or event
            elif line.startswith("data:"):
                data_lines.append(line.removeprefix("data:").strip())
        if not data_lines:
            continue
        try:
            data = parse_json_value("\n".join(data_lines))
        except ValueError:
            data = {"message": "\n".join(data_lines)}
        if isinstance(data, dict):
            events.append(_task_event_from_data(event, data))
    return events


def _task_event_from_data(event: str, data: Mapping[str, JsonValue]) -> TaskLifecycleEvent:
    task = None
    task_value = data.get("task")
    if isinstance(task_value, dict):
        try:
            task = shared.tasks.Task.model_validate(task_value)
        except ValueError:
            task = None
    return TaskLifecycleEvent(event=event, data=dict(data), task=task)


__all__ = [
    "FunctionCall",
    "Task",
    "TaskBatch",
    "TaskClient",
    "TaskControlClient",
    "TaskLifecycleEvent",
    "TaskOperationError",
    "TaskResult",
    "TaskSubscription",
]
