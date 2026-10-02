"""Which invocation the code running right now belongs to.

The SDK reads it to choose a spawned call's parent, and an artifact reads it to
choose what it belongs to. A container can serve several calls at once, so a
process global cannot answer: two handlers running together would each read
whichever wrote last. Each invocation carries its identity in a context
variable, which threads and coroutines both propagate.

`TASK_ID` in the environment is the outer default. A process started for one
task names it there, and a child process inherits it; the context variable
narrows it for each concurrent invocation.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from lazycloud._shared.env import ROOT_TASK_ID_ENV, TASK_ID_ENV

_TASK_ID: ContextVar[str] = ContextVar("lazycloud_task_id", default="")
_ROOT_TASK_ID: ContextVar[str] = ContextVar("lazycloud_root_task_id", default="")


@contextmanager
def task_context(task_id: str, root_task_id: str = "") -> Iterator[None]:
    """Run the enclosed work as the named invocation.

    Reset on the way out rather than restored to a captured value: a nested
    scope must give back exactly the token it took, or an inner exit can leave
    an outer scope reading an identity it never set.
    """

    task_token = _TASK_ID.set(task_id)
    root_token = _ROOT_TASK_ID.set(root_task_id or task_id)
    try:
        yield
    finally:
        _ROOT_TASK_ID.reset(root_token)
        _TASK_ID.reset(task_token)


def current_task_id() -> str:
    """The invocation this code is running for, or empty outside one."""

    return _TASK_ID.get() or os.environ.get(TASK_ID_ENV, "").strip()


def current_root_task_id() -> str:
    """The root of the call graph this invocation belongs to.

    Falls back to the invocation itself, because a call nobody spawned is the
    root of its own graph.
    """

    return _ROOT_TASK_ID.get() or os.environ.get(ROOT_TASK_ID_ENV, "").strip() or current_task_id()


__all__ = [
    "current_root_task_id",
    "current_task_id",
    "task_context",
]
