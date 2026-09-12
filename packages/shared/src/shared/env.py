from __future__ import annotations

import os
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar

TRUTHY_ENV_VALUES: frozenset[str] = frozenset({"1", "true", "yes", "on"})
IMPORTING_USER_CODE_ENV = "IMPORTING_USER_CODE"
CONTAINER_ID_ENV = "CONTAINER_ID"
CONTAINER_HOSTNAME_ENV = "CONTAINER_HOSTNAME"
CHECKPOINT_ENABLED_ENV = "CHECKPOINT_ENABLED"
ENDPOINT_INSTANCE_LOCK_ENV = "ENDPOINT_INSTANCE_LOCK"
ENDPOINT_SERVE_HOST_ENV = "ENDPOINT_SERVE_HOST"
ENDPOINT_SERVE_LOCK_ENV = "ENDPOINT_SERVE_LOCK"
ENDPOINT_WORKERS_ENV = "ENDPOINT_WORKERS"
GATEWAY_GRPC_HOST_ENV = "GATEWAY_GRPC_HOST"
GATEWAY_GRPC_PORT_ENV = "GATEWAY_GRPC_PORT"
GATEWAY_GRPC_TLS_ENV = "GATEWAY_GRPC_TLS"
GATEWAY_HTTP_HOST_ENV = "GATEWAY_HTTP_HOST"
GATEWAY_HTTP_PORT_ENV = "GATEWAY_HTTP_PORT"
GATEWAY_HTTP_TLS_ENV = "GATEWAY_HTTP_TLS"
GATEWAY_HTTP_URL_ENV = "GATEWAY_HTTP_URL"
FUNCTION_CONCURRENCY_ENV = "FUNCTION_CONCURRENCY"
FUNCTION_IN_PROCESS_ENV = "FUNCTION_IN_PROCESS"
GATEWAY_TOKEN_ENV = "GATEWAY_TOKEN"
HOT_RELOAD_ENV = "HOT_RELOAD"
HOT_RELOAD_DIR_ENV = "HOT_RELOAD_DIR"
KEEP_WARM_SECONDS_ENV = "KEEP_WARM_SECONDS"
LIFECYCLE_HOOKS_ENV = "LIFECYCLE_HOOKS"
APP_ID_ENV = "APP_ID"
STORAGE_AVAILABLE_ENV = "STORAGE_AVAILABLE"
STUB_ID_ENV = "STUB_ID"
STUB_TYPE_ENV = "STUB_TYPE"
ROOT_TASK_ID_ENV = "ROOT_TASK_ID"
TASK_ID_ENV = "TASK_ID"
WORKSPACE_ID_ENV = "WORKSPACE_ID"
WORKSPACE_NAME_ENV = "WORKSPACE_NAME"


def truthy_env_value(value: str | None) -> bool:
    return (value or "").strip().lower() in TRUTHY_ENV_VALUES


def parse_environment(values: Iterable[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        key, separator, item = value.partition("=")
        if separator:
            result[key] = item
    return result


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
    return truthy_env_value(source.get(IMPORTING_USER_CODE_ENV))


__all__ = [
    "APP_ID_ENV",
    "CHECKPOINT_ENABLED_ENV",
    "CONTAINER_HOSTNAME_ENV",
    "CONTAINER_ID_ENV",
    "ENDPOINT_INSTANCE_LOCK_ENV",
    "ENDPOINT_SERVE_HOST_ENV",
    "ENDPOINT_SERVE_LOCK_ENV",
    "ENDPOINT_WORKERS_ENV",
    "FUNCTION_CONCURRENCY_ENV",
    "FUNCTION_IN_PROCESS_ENV",
    "GATEWAY_GRPC_HOST_ENV",
    "GATEWAY_GRPC_PORT_ENV",
    "GATEWAY_GRPC_TLS_ENV",
    "GATEWAY_HTTP_HOST_ENV",
    "GATEWAY_HTTP_PORT_ENV",
    "GATEWAY_HTTP_TLS_ENV",
    "GATEWAY_HTTP_URL_ENV",
    "GATEWAY_TOKEN_ENV",
    "HOT_RELOAD_DIR_ENV",
    "HOT_RELOAD_ENV",
    "IMPORTING_USER_CODE_ENV",
    "KEEP_WARM_SECONDS_ENV",
    "LIFECYCLE_HOOKS_ENV",
    "ROOT_TASK_ID_ENV",
    "STORAGE_AVAILABLE_ENV",
    "STUB_ID_ENV",
    "STUB_TYPE_ENV",
    "TASK_ID_ENV",
    "TRUTHY_ENV_VALUES",
    "WORKSPACE_ID_ENV",
    "WORKSPACE_NAME_ENV",
    "importing_user_code",
    "importing_user_code_now",
    "parse_environment",
    "truthy_env_value",
]
