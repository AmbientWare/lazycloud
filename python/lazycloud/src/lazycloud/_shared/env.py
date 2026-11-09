from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar

IMPORTING_USER_CODE_ENV = "IMPORTING_USER_CODE"
CONTAINER_ID_ENV = "CONTAINER_ID"
ROOT_TASK_ID_ENV = "ROOT_TASK_ID"
TASK_ID_ENV = "TASK_ID"


_IMPORTING_USER_CODE: ContextVar[bool] = ContextVar(
    "lazycloud_importing_user_code",
    default=False,
)


@contextmanager
def importing_user_code() -> Iterator[None]:
    """Mark the enclosed import as user code being loaded, not run.

    Held per context rather than in the environment. A container that imports a
    handler while another invocation is mid-flight would otherwise flip a
    process-wide flag under it, and the SDK reads that flag to decide whether a
    call is a real invocation or a decorator firing during import — so the wrong
    answer there turns a user's call into a silent no-op.
    """

    token = _IMPORTING_USER_CODE.set(True)
    try:
        yield
    finally:
        _IMPORTING_USER_CODE.reset(token)


def importing_user_code_now(env: Mapping[str, str] | None = None) -> bool:
    """Whether user code is being imported in this context.

    The environment is still consulted, because a process launched purely to
    import — the CLI resolving a handler reference — says so there and never
    enters the context manager.
    """

    if _IMPORTING_USER_CODE.get():
        return True
    source = env if env is not None else os.environ
    return source.get(IMPORTING_USER_CODE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


__all__ = [
    "CONTAINER_ID_ENV",
    "IMPORTING_USER_CODE_ENV",
    "ROOT_TASK_ID_ENV",
    "TASK_ID_ENV",
    "importing_user_code",
    "importing_user_code_now",
]
