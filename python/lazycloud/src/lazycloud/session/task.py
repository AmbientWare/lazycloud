from __future__ import annotations

import base64
import io
import math
import pickle
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Generic, TypeVar, cast
from uuid import UUID

import cloudpickle
from pydantic import JsonValue

from lazycloud._shared.task_context import current_task_id
from lazycloud.aio import to_thread
from lazycloud.clients.api import ApiClient, ApiError, is_transient
from lazycloud.contracts.api import (
    ContainerLogEntry,
    Encoding,
    LogEntry,
    Payload,
    TaskInput,
    TaskPendingProgress,
    TaskStatus,
)
from lazycloud.contracts.api import Task as TaskView
from lazycloud.control import api_client, require_workspace, resolve_control_client_config
from lazycloud.exceptions import RemoteTaskError, TaskCancelledError, TaskNotFoundError
from lazycloud.progress import PendingProgressReporter, progress_observed
from lazycloud.terminal import Terminal

R = TypeVar("R")
T = TypeVar("T")

# The API holds a status read for at most this long.
WAIT_SECONDS = 30
# Tasks one waitTasks request reads; the API rejects more.
MAX_WAIT_BATCH = 1000
# Calls one input may depend on; the API rejects more.
MAX_DEPENDENCIES = 100
# How often a wait reads a queued task while a progress callback listens.
PENDING_POLL_SECONDS = 1
# How long transient failures may continue, counted from the first one,
# before a wait gives up. A task keeps running when the client gives up, so a
# server restart or network blip must not end a caller's wait.
_TRANSIENT_BUDGET_SECONDS = 600.0
TERMINAL_STATUSES = frozenset({TaskStatus.succeeded, TaskStatus.failed, TaskStatus.cancelled})


@dataclass(frozen=True, slots=True)
class TaskResult(Generic[R]):
    """A task's state with its value; the value is None until it succeeds."""

    task: TaskView
    value: R

    @property
    def id(self) -> str:
        return str(self.task.id)

    @property
    def status(self) -> TaskStatus:
        return self.task.status

    @property
    def ok(self) -> bool:
        return self.task.status is TaskStatus.succeeded

    @property
    def error(self) -> str:
        failure = self.task.failure
        if failure is None:
            return ""
        return f"{failure.type}: {failure.message}" if failure.type else failure.message

    @property
    def exit_code(self) -> int | None:
        """Always None: tasks fail with a typed failure rather than an exit code."""
        return None


@dataclass(frozen=True, slots=True)
class TaskLifecycleEvent:
    event: str
    data: dict[str, JsonValue]
    task: TaskView | None = None


@dataclass(frozen=True, slots=True)
class TaskSubscription:
    task_id: str
    events: tuple[TaskLifecycleEvent, ...]

    def __iter__(self) -> Iterator[TaskLifecycleEvent]:
        return iter(self.events)

    @property
    def latest(self) -> TaskLifecycleEvent | None:
        return self.events[-1] if self.events else None


@dataclass(slots=True)
class Task:
    """A submitted task, addressed by id within its workspace."""

    task_id: str
    workspace: str
    client: ApiClient

    @classmethod
    def from_id(cls, task_id: str, *, workspace: str | None = None) -> Task:
        """Reconnect to a task using the active profile and selected workspace."""
        config = resolve_control_client_config(workspace=workspace)
        return cls(task_id=task_id, workspace=require_workspace(config), client=api_client(config))

    @property
    def _id(self) -> UUID:
        try:
            return UUID(self.task_id)
        except ValueError:
            raise TaskNotFoundError(self.task_id) from None

    def get(self) -> TaskView:
        """The task's current state."""
        return self._read(lambda: self.client.get_task(self.workspace, self._id))

    def view(self) -> TaskView:
        return self.get()

    @property
    def pending_progress(self) -> TaskPendingProgress | None:
        """Why a queued task has not started; None once it runs."""
        return self.view().pending

    def result(
        self, *, wait: bool = False, timeout_seconds: float | None = None
    ) -> TaskResult[Payload | None]:
        """The task with its stored result payload, waiting for it to finish when `wait`."""
        if wait:
            return self.wait(timeout_seconds=timeout_seconds)
        return self._with_result(self.get())

    def wait(self, *, timeout_seconds: float | None = None) -> TaskResult[Payload | None]:
        """Hold until the task finishes and return it with its result payload.

        Raises TimeoutError when `timeout_seconds` passes first; the task keeps
        running.
        """
        return self._with_result(self.wait_view(timeout_seconds=timeout_seconds))

    async def async_wait(
        self, *, timeout_seconds: float | None = None
    ) -> TaskResult[Payload | None]:
        return await to_thread(self.wait, timeout_seconds=timeout_seconds)

    def wait_view(self, *, timeout_seconds: float | None = None) -> TaskView:
        deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
        reporter = PendingProgressReporter(terminal=Terminal(default_enabled=False))
        view: TaskView | None = None
        while True:
            hold = WAIT_SECONDS
            if progress_observed() and (view is None or view.status is TaskStatus.queued):
                hold = PENDING_POLL_SECONDS
            if deadline is not None:
                hold = min(hold, max(int(deadline - time.monotonic()), 0))
            view = self._read(
                lambda hold=hold: self.client.get_task(self.workspace, self._id, wait_seconds=hold)
            )
            reporter.update(self.task_id, view.pending)
            if view.status in TERMINAL_STATUSES:
                return view
            if deadline is not None and time.monotonic() >= deadline:
                msg = f"task {self.task_id} did not finish within {timeout_seconds} seconds"
                raise TimeoutError(msg)

    def outcome(self, view: TaskView) -> Any:
        """The decoded value of a finished task, or its failure raised locally."""
        return finished_value(self._with_result(view))

    def logs(self, *, limit: int = 100, cursor: str | int | None = None) -> list[LogEntry]:
        """Stored log entries: the last `limit`, or the first `limit` after `cursor`.

        An entry's `id` is the cursor for the entries that follow it.
        """
        if cursor is None:
            stream = self.client.stream_task_logs(self.workspace, self._id, tail=limit)
        else:
            stream = self.client.stream_task_logs(self.workspace, self._id, after=int(cursor))
        entries: list[LogEntry] = []
        for entry in stream:
            entries.append(entry)
            if len(entries) >= limit:
                break
        return entries

    def output(self, *, limit: int = 100, cursor: str | int | None = None) -> str:
        return "\n".join(entry.data for entry in self.logs(limit=limit, cursor=cursor))

    def subscribe(self) -> TaskSubscription:
        """The task's lifecycle so far: one `status` event with its current state."""
        view = self.get()
        event = TaskLifecycleEvent(
            event="status", data=cast(dict[str, JsonValue], view.model_dump(mode="json")), task=view
        )
        return TaskSubscription(task_id=self.task_id, events=(event,))

    def cancel(self) -> TaskView:
        return self._read(lambda: self.client.cancel_task(self.workspace, self._id))

    def follow_logs(self, emit: Callable[[LogEntry], None], *, after: int = 0) -> None:
        """Deliver log entries after `after` until the task finishes, resuming dropped streams."""
        for entry in follow_log_stream(
            lambda after: self.client.stream_task_logs(
                self.workspace, self._id, after=after, follow=True
            ),
            after=after,
        ):
            emit(entry)

    def _with_result(self, view: TaskView) -> TaskResult[Payload | None]:
        if view.status is not TaskStatus.succeeded:
            return TaskResult(view, None)
        payload = self._read(lambda: self.client.get_task_result(self.workspace, self._id))
        return TaskResult(view, payload)

    def _read(self, call: Callable[[], T]) -> T:
        return retry_transient(call, not_found=lambda: TaskNotFoundError(self.task_id))


@dataclass(slots=True)
class FunctionCall(Generic[R]):
    """A spawned function task whose value can be collected later.

    Passed as an argument of another remote call, it arrives there as its
    value: the platform starts the dependent task after this one succeeds.
    """

    task: Task

    @property
    def task_id(self) -> str:
        return self.task.task_id

    def result(
        self, *, wait: bool = False, timeout_seconds: float | None = None
    ) -> TaskResult[R | None]:
        """The task with its decoded value, which is None unless it succeeded."""
        result = self.task.result(wait=wait, timeout_seconds=timeout_seconds)
        if not result.ok or result.value is None:
            return TaskResult(result.task, None)
        return TaskResult(result.task, cast(R, decode_payload(result.value)))

    def get(self, *, timeout_seconds: float | None = None) -> R:
        """The call's value; a failure raises the remote exception."""
        view = self.task.wait_view(timeout_seconds=timeout_seconds)
        return cast(R, self.task.outcome(view))

    def logs(self, *, limit: int = 100, cursor: str | int | None = None) -> list[LogEntry]:
        return self.task.logs(limit=limit, cursor=cursor)

    def output(self, *, limit: int = 100, cursor: str | int | None = None) -> str:
        return self.task.output(limit=limit, cursor=cursor)

    def subscribe(self) -> TaskSubscription:
        return self.task.subscribe()

    def cancel(self) -> TaskView:
        return self.task.cancel()

    def rerun(self) -> FunctionCall[R]:
        """Submit this call's input again, to the release it ran on."""
        task = self.task
        view = task._read(lambda: task.client.rerun_task(task.workspace, task._id))
        return FunctionCall(Task(str(view.id), task.workspace, task.client))

    def __reduce__(self) -> tuple[object, ...]:
        # Remote calls pickle a FunctionCall by reference; any other pickle
        # would carry the API client and its token.
        msg = "a FunctionCall can be passed only as an argument of a remote call"
        raise TypeError(msg)

    @classmethod
    def gather(
        cls,
        *calls: FunctionCall[Any],
        timeout_seconds: float | None = None,
        return_exceptions: bool = False,
    ) -> list[Any]:
        """The calls' values in order; `timeout_seconds` bounds the whole wait."""
        results: list[Any] = []
        finished = wait_in_order([call.task for call in calls], timeout_seconds=timeout_seconds)
        for _ in calls:
            try:
                result = next(finished)
            except Exception as exc:
                # The wait itself failed, so every call left shares the failure.
                if not return_exceptions:
                    raise
                results.extend([exc] * (len(calls) - len(results)))
                break
            try:
                if isinstance(result, Exception):
                    raise result
                results.append(finished_value(result))
            except Exception as exc:
                if not return_exceptions:
                    raise
                results.append(exc)
        return results


def task_input(
    args: tuple[Any, ...] | list[Any], kwargs: Mapping[str, Any], *, workspace: str
) -> TaskInput:
    """Cloudpickled arguments, with each FunctionCall written as a reference to its task.

    The runner resolves `("function_call", task_id)` to the call's value, and
    `depends_on` tells the platform to start the task after those succeed.
    """
    depends_on: dict[str, UUID] = {}

    def persistent_id(value: object) -> tuple[str, str] | None:
        if not isinstance(value, FunctionCall):
            return None
        if value.task.workspace != workspace:
            msg = (
                f"task {value.task_id} belongs to workspace {value.task.workspace}, not {workspace}"
            )
            raise ValueError(msg)
        depends_on.setdefault(value.task_id, UUID(value.task_id))
        return ("function_call", value.task_id)

    stream = io.BytesIO()
    pickler = cloudpickle.CloudPickler(stream)
    pickler.persistent_id = persistent_id  # type: ignore[method-assign]
    pickle.Pickler.dump(pickler, {"args": list(args), "kwargs": dict(kwargs)})
    if len(depends_on) > MAX_DEPENDENCIES:
        raise ValueError(f"a call can depend on at most {MAX_DEPENDENCIES} other calls")
    payload: dict[str, Any] = {
        "encoding": Encoding.cloudpickle,
        "data": base64.b64encode(stream.getvalue()),
    }
    if depends_on:
        payload["depends_on"] = list(depends_on.values())
    return TaskInput.model_validate(payload)


def parent_task_id() -> UUID | None:
    """The task this code runs in, which becomes the parent of the calls it makes."""
    try:
        return UUID(current_task_id())
    except ValueError:
        return None


EntryT = TypeVar("EntryT", LogEntry, ContainerLogEntry)


def follow_log_stream(
    open_stream: Callable[[int], Iterator[EntryT]], *, after: int = 0
) -> Iterator[EntryT]:
    """Yield a followed log stream, reopening after the last entry when it drops.

    `open_stream` receives the id of the last delivered entry, `after` at
    first. Non-transient failures, and transient ones past the retry budget,
    raise.
    """
    cursor = after
    failures = 0
    failing_since = 0.0
    while True:
        try:
            for entry in open_stream(cursor):
                cursor = entry.id
                failures = 0
                yield entry
            return
        except Exception as exc:
            failures += 1
            if failures == 1:
                failing_since = time.monotonic()
            if not is_transient(exc) or retry_budget_spent(failing_since):
                raise
            retry_backoff(failures)


def retry_transient(call: Callable[[], T], *, not_found: Callable[[], Exception]) -> T:
    """Run `call`, retrying transient failures within the budget; a 404 raises `not_found()`."""
    failures = 0
    failing_since = 0.0
    while True:
        try:
            return call()
        except Exception as exc:
            if isinstance(exc, ApiError) and exc.status_code == 404:
                raise not_found() from exc
            failures += 1
            if failures == 1:
                failing_since = time.monotonic()
            if not is_transient(exc) or retry_budget_spent(failing_since):
                raise
        retry_backoff(failures)


def wait_in_order(
    tasks: Sequence[Task], *, timeout_seconds: float | None = None
) -> Iterator[TaskResult[Payload | None] | Exception]:
    """Yield each task finished with its result payload, in input order.

    A task comes as soon as it and every task before it have finished; one
    request reads up to MAX_WAIT_BATCH tasks. In place of a task comes
    TaskNotFoundError when its workspace has no such task, and TimeoutError
    when `timeout_seconds` passed before it finished. The tasks keep running.
    """
    deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
    known: dict[int, TaskResult[Payload | None] | Exception] = {}
    waiting: dict[int, UUID] = {}
    for n, task in enumerate(tasks):
        try:
            waiting[n] = UUID(task.task_id)
        except ValueError:
            known[n] = TaskNotFoundError(task.task_id)
    # Reports why the first unfinished task is queued, as a single wait does.
    watched: tuple[int, PendingProgressReporter] | None = None
    for head, task in enumerate(tasks):
        while head not in known:
            hold = PENDING_POLL_SECONDS if progress_observed() else WAIT_SECONDS
            if deadline is not None:
                hold = min(hold, max(math.ceil(deadline - time.monotonic()), 0))
            _wait_round(tasks, waiting, known, hold)
            if head in known:
                break
            if deadline is not None and time.monotonic() >= deadline:
                for n in waiting:
                    known[n] = TimeoutError(
                        f"task {tasks[n].task_id} did not finish within {timeout_seconds} seconds"
                    )
                waiting.clear()
            elif progress_observed():
                if watched is None or watched[0] != head:
                    watched = (
                        head,
                        PendingProgressReporter(terminal=Terminal(default_enabled=False)),
                    )
                watched[1].update(task.task_id, task.get().pending)
        if watched is not None and watched[0] == head:
            watched[1].update(task.task_id, None)
        yield known.pop(head)


def _wait_round(
    tasks: Sequence[Task],
    waiting: dict[int, UUID],
    known: dict[int, TaskResult[Payload | None] | Exception],
    hold: int,
) -> None:
    """Read every waiting task once, moving each finished one to `known`.

    Only the batch holding the earliest waiting task holds the request, for
    up to `hold` seconds, and only when no other batch found a finished task.
    """
    sessions: dict[tuple[int, str], dict[UUID, list[int]]] = {}
    for n, task_id in waiting.items():
        task = tasks[n]
        sessions.setdefault((id(task.client), task.workspace), {}).setdefault(task_id, []).append(n)
    batches: list[dict[UUID, list[int]]] = []
    for ids in sessions.values():
        items = list(ids.items())
        for start in range(0, len(items), MAX_WAIT_BATCH):
            batches.append(dict(items[start : start + MAX_WAIT_BATCH]))
    found = False
    for batch in [*batches[1:], batches[0]]:
        wait = 0 if found or batch is not batches[0] else hold
        found = _read_batch(tasks, batch, wait, waiting, known) or found


def _read_batch(
    tasks: Sequence[Task],
    batch: dict[UUID, list[int]],
    wait: int,
    waiting: dict[int, UUID],
    known: dict[int, TaskResult[Payload | None] | Exception],
) -> bool:
    """Read one batch of a session's tasks; return whether any finished."""
    first = tasks[next(iter(batch.values()))[0]]
    ids = list(batch)
    try:
        response = retry_transient(
            lambda: first.client.wait_tasks(first.workspace, ids, wait_seconds=wait),
            not_found=lambda: TaskNotFoundError(str(ids[0])),
        )
    except TaskNotFoundError as exc:
        # The API names the first task it does not have; the rest are read
        # again next round.
        cause = exc.__cause__
        message = cause.message if isinstance(cause, ApiError) else ""
        missing = next((task_id for task_id in ids if str(task_id) in message), None)
        if missing is None:
            raise
        for n in batch[missing]:
            known[n] = TaskNotFoundError(str(missing))
            del waiting[n]
        return True
    for finished in response.tasks:
        indices = batch[finished.task.id]
        result = (
            tasks[indices[0]]._with_result(finished.task)
            if finished.result_omitted
            else TaskResult(finished.task, finished.result)
        )
        for n in indices:
            known[n] = result
            del waiting[n]
    return bool(response.tasks)


def finished_value(result: TaskResult[Payload | None]) -> Any:
    """A finished task's decoded value, or its failure raised locally."""
    view = result.task
    if view.status is TaskStatus.succeeded and result.value is not None:
        return decode_payload(result.value)
    if view.status is TaskStatus.cancelled:
        raise TaskCancelledError(str(view.id))
    if view.status is TaskStatus.failed:
        raise_task_failure(view)
    msg = f"task {view.id} has not finished with a result: {view.status.value}"
    raise RuntimeError(msg)


def decode_payload(payload: Payload) -> Any:
    if payload.encoding is Encoding.json:
        return payload.value
    if payload.data is None:
        raise ValueError("cloudpickle payload has no data")
    return pickle.loads(payload.data)


def raise_task_failure(view: TaskView) -> None:
    failure = view.failure
    task_id = str(view.id)
    if failure is None:
        raise RemoteTaskError(
            task_id, kind="system", type=None, message="no failure recorded", traceback=None
        )
    remote = RemoteTaskError(
        task_id,
        kind=failure.kind.value,
        type=failure.type,
        message=failure.message,
        traceback=failure.traceback,
    )
    if failure.exception:
        try:
            restored = pickle.loads(failure.exception)
        except Exception:
            restored = None
        if isinstance(restored, BaseException):
            raise restored from remote
    raise remote


def retry_budget_spent(failing_since: float) -> bool:
    return time.monotonic() - failing_since >= _TRANSIENT_BUDGET_SECONDS


def retry_backoff(failures: int) -> None:
    time.sleep(min(0.5 * 2 ** (failures - 1), 8.0))


__all__ = [
    "TERMINAL_STATUSES",
    "FunctionCall",
    "Task",
    "TaskLifecycleEvent",
    "TaskResult",
    "TaskSubscription",
    "decode_payload",
    "finished_value",
    "follow_log_stream",
    "parent_task_id",
    "raise_task_failure",
    "retry_backoff",
    "retry_budget_spent",
    "retry_transient",
    "task_input",
    "wait_in_order",
]
