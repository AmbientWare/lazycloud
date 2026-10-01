from __future__ import annotations

import pickle
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Generic, TypeVar, cast
from uuid import UUID

from shared.api import Encoding, LogEntry, Payload, TaskStatus
from shared.api import Task as TaskView

from lazycloud.clients.api import ApiClient, ApiError, is_transient
from lazycloud.control import api_client, require_workspace, resolve_control_client_config
from lazycloud.exceptions import RemoteTaskError, TaskCancelledError, TaskNotFoundError

R = TypeVar("R")
T = TypeVar("T")

# The API holds a status read for at most this long.
WAIT_SECONDS = 30
# How long transient failures may continue, counted from the first one,
# before a wait gives up. A task keeps running when the client gives up, so a
# server restart or network blip must not end a caller's wait.
_TRANSIENT_BUDGET_SECONDS = 600.0
_TERMINAL_STATUSES = frozenset({TaskStatus.succeeded, TaskStatus.failed, TaskStatus.cancelled})


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
        return UUID(self.task_id)

    def status(self) -> TaskView:
        return self._read(lambda: self.client.get_task(self.workspace, self._id))

    def wait(self, *, timeout_seconds: float | None = None) -> TaskView:
        """Hold until the task finishes and return its final state.

        Raises TimeoutError when `timeout_seconds` passes first; the task keeps
        running.
        """
        deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
        while True:
            hold = WAIT_SECONDS
            if deadline is not None:
                hold = min(hold, max(int(deadline - time.monotonic()), 0))
            view = self._read(
                lambda hold=hold: self.client.get_task(self.workspace, self._id, wait_seconds=hold)
            )
            if view.status in _TERMINAL_STATUSES:
                return view
            if deadline is not None and time.monotonic() >= deadline:
                msg = f"task {self.task_id} did not finish within {timeout_seconds} seconds"
                raise TimeoutError(msg)

    def result(self, *, timeout_seconds: float | None = None) -> Any:
        """The task's return value, or its failure raised locally."""
        return self.outcome(self.wait(timeout_seconds=timeout_seconds))

    def outcome(self, view: TaskView) -> Any:
        if view.status is TaskStatus.succeeded:
            return decode_payload(
                self._read(lambda: self.client.get_task_result(self.workspace, self._id))
            )
        if view.status is TaskStatus.cancelled:
            raise TaskCancelledError(self.task_id)
        if view.status is TaskStatus.failed:
            raise_task_failure(view)
        msg = f"task {self.task_id} has not finished: {view.status.value}"
        raise RuntimeError(msg)

    def cancel(self) -> TaskView:
        return self._read(lambda: self.client.cancel_task(self.workspace, self._id))

    def logs(self, *, after: int = 0, follow: bool = False) -> Iterator[LogEntry]:
        return self.client.stream_task_logs(self.workspace, self._id, after=after, follow=follow)

    def follow_logs(self, emit: Callable[[LogEntry], None]) -> None:
        """Deliver log entries until the task finishes, resuming after dropped streams."""
        cursor = 0
        failures = 0
        while True:
            try:
                for entry in self.logs(after=cursor, follow=True):
                    cursor = entry.id
                    failures = 0
                    emit(entry)
                return
            except Exception as exc:
                failures += 1
                if failures == 1:
                    failing_since = time.monotonic()
                if not is_transient(exc) or retry_budget_spent(failing_since):
                    raise
                retry_backoff(failures)

    def _read(self, call: Callable[[], T]) -> T:
        failures = 0
        while True:
            try:
                return call()
            except Exception as exc:
                if isinstance(exc, ApiError) and exc.status_code == 404:
                    raise TaskNotFoundError(self.task_id) from exc
                failures += 1
                if failures == 1:
                    failing_since = time.monotonic()
                if not is_transient(exc) or retry_budget_spent(failing_since):
                    raise
            retry_backoff(failures)


@dataclass(slots=True)
class FunctionCall(Generic[R]):
    """A spawned function task whose value can be collected later."""

    task: Task

    @property
    def task_id(self) -> str:
        return self.task.task_id

    def get(self, *, timeout_seconds: float | None = None) -> R:
        return cast(R, self.task.result(timeout_seconds=timeout_seconds))

    def status(self) -> TaskView:
        return self.task.status()

    def cancel(self) -> TaskView:
        return self.task.cancel()

    def logs(self, *, after: int = 0, follow: bool = False) -> Iterator[LogEntry]:
        return self.task.logs(after=after, follow=follow)

    def __reduce__(self) -> tuple[object, ...]:
        msg = "a FunctionCall cannot be passed to a task; pass its result instead"
        raise TypeError(msg)

    @classmethod
    def gather(
        cls,
        *calls: FunctionCall[Any],
        timeout_seconds: float | None = None,
        return_exceptions: bool = False,
    ) -> list[Any]:
        results: list[Any] = []
        for call in calls:
            try:
                results.append(call.get(timeout_seconds=timeout_seconds))
            except Exception as exc:
                if not return_exceptions:
                    raise
                results.append(exc)
        return results


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
    "FunctionCall",
    "Task",
    "decode_payload",
    "raise_task_failure",
    "retry_backoff",
    "retry_budget_spent",
]
