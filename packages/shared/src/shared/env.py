from __future__ import annotations

import os
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager

from shared.enums import StringEnum

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
GATEWAY_TOKEN_ENV = "GATEWAY_TOKEN"
HOT_RELOAD_ENV = "HOT_RELOAD"
HOT_RELOAD_DIR_ENV = "HOT_RELOAD_DIR"
INPUTS_ENV = "INPUTS"
KEEP_WARM_SECONDS_ENV = "KEEP_WARM_SECONDS"
LIFECYCLE_HOOKS_ENV = "LIFECYCLE_HOOKS"
OUTPUTS_ENV = "OUTPUTS"
APP_ID_ENV = "APP_ID"
STORAGE_AVAILABLE_ENV = "STORAGE_AVAILABLE"
STUB_ID_ENV = "STUB_ID"
STUB_TYPE_ENV = "STUB_TYPE"
ROOT_TASK_ID_ENV = "ROOT_TASK_ID"
TASK_ID_ENV = "TASK_ID"
TASK_QUEUE_SERVE_LOCK_ENV = "TASK_QUEUE_SERVE_LOCK"
TASK_QUEUE_RETRY_FOR_ENV = "TASK_QUEUE_RETRY_FOR"
TASK_QUEUE_WORKERS_ENV = "TASK_QUEUE_WORKERS"
WORKSPACE_ID_ENV = "WORKSPACE_ID"
WORKSPACE_NAME_ENV = "WORKSPACE_NAME"
WORKER_REPOSITORY_URL_ENV = "WORKER_REPOSITORY_URL"
# Where a worker asks the agent to resolve a tailnet peer. Set only when the
# node runs a tailnet, since a worker without it simply dials names directly.
WORKER_PEER_RESOLVER_ADDRESS_ENV = "WORKER_PEER_RESOLVER_ADDRESS"
WORKER_TAILNET_DNS_SUFFIX_ENV = "WORKER_TAILNET_DNS_SUFFIX"


class ExecutionEnvVar(StringEnum):
    ImportingUserCode = IMPORTING_USER_CODE_ENV
    ContainerId = CONTAINER_ID_ENV
    ContainerHostname = CONTAINER_HOSTNAME_ENV
    CheckpointEnabled = CHECKPOINT_ENABLED_ENV
    EndpointInstanceLock = ENDPOINT_INSTANCE_LOCK_ENV
    EndpointServeHost = ENDPOINT_SERVE_HOST_ENV
    EndpointServeLock = ENDPOINT_SERVE_LOCK_ENV
    EndpointWorkers = ENDPOINT_WORKERS_ENV
    GatewayGrpcHost = GATEWAY_GRPC_HOST_ENV
    GatewayGrpcPort = GATEWAY_GRPC_PORT_ENV
    GatewayGrpcTls = GATEWAY_GRPC_TLS_ENV
    GatewayHttpHost = GATEWAY_HTTP_HOST_ENV
    GatewayHttpPort = GATEWAY_HTTP_PORT_ENV
    GatewayHttpTls = GATEWAY_HTTP_TLS_ENV
    GatewayHttpUrl = GATEWAY_HTTP_URL_ENV
    GatewayToken = GATEWAY_TOKEN_ENV
    HotReload = HOT_RELOAD_ENV
    HotReloadDir = HOT_RELOAD_DIR_ENV
    Inputs = INPUTS_ENV
    KeepWarmSeconds = KEEP_WARM_SECONDS_ENV
    LifecycleHooks = LIFECYCLE_HOOKS_ENV
    Outputs = OUTPUTS_ENV
    AppId = APP_ID_ENV
    StorageAvailable = STORAGE_AVAILABLE_ENV
    StubId = STUB_ID_ENV
    StubType = STUB_TYPE_ENV
    RootTaskId = ROOT_TASK_ID_ENV
    TaskId = TASK_ID_ENV
    TaskQueueServeLock = TASK_QUEUE_SERVE_LOCK_ENV
    TaskQueueRetryFor = TASK_QUEUE_RETRY_FOR_ENV
    TaskQueueWorkers = TASK_QUEUE_WORKERS_ENV
    WorkspaceId = WORKSPACE_ID_ENV
    WorkspaceName = WORKSPACE_NAME_ENV


def truthy_env_value(value: str | None) -> bool:
    return (value or "").strip().lower() in TRUTHY_ENV_VALUES


@contextmanager
def importing_user_code(
    env: MutableMapping[str, str] | None = None,
) -> Iterator[None]:
    source = env if env is not None else os.environ
    previous = source.get(IMPORTING_USER_CODE_ENV)
    source[IMPORTING_USER_CODE_ENV] = "true"
    try:
        yield
    finally:
        if previous is None:
            source.pop(IMPORTING_USER_CODE_ENV, None)
        else:
            source[IMPORTING_USER_CODE_ENV] = previous


def no_gateway_origin() -> str:
    """The origin a service resolves before one is configured.

    A callable rather than a bare string because the control plane publishes
    where it is reachable at runtime; holding the value would freeze whatever
    was true at construction.
    """
    return ""


__all__ = [
    "APP_ID_ENV",
    "CHECKPOINT_ENABLED_ENV",
    "CONTAINER_HOSTNAME_ENV",
    "CONTAINER_ID_ENV",
    "ENDPOINT_INSTANCE_LOCK_ENV",
    "ENDPOINT_SERVE_HOST_ENV",
    "ENDPOINT_SERVE_LOCK_ENV",
    "ENDPOINT_WORKERS_ENV",
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
    "INPUTS_ENV",
    "KEEP_WARM_SECONDS_ENV",
    "LIFECYCLE_HOOKS_ENV",
    "OUTPUTS_ENV",
    "ROOT_TASK_ID_ENV",
    "STORAGE_AVAILABLE_ENV",
    "STUB_ID_ENV",
    "STUB_TYPE_ENV",
    "TASK_ID_ENV",
    "TASK_QUEUE_RETRY_FOR_ENV",
    "TASK_QUEUE_SERVE_LOCK_ENV",
    "TASK_QUEUE_WORKERS_ENV",
    "TRUTHY_ENV_VALUES",
    "WORKER_PEER_RESOLVER_ADDRESS_ENV",
    "WORKER_REPOSITORY_URL_ENV",
    "WORKER_TAILNET_DNS_SUFFIX_ENV",
    "WORKSPACE_ID_ENV",
    "WORKSPACE_NAME_ENV",
    "ExecutionEnvVar",
    "importing_user_code",
    "no_gateway_origin",
    "truthy_env_value",
]
