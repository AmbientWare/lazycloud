from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from pydantic import JsonValue
from shared.autoscaling import QueueDepthAutoscaler
from shared.lifecycle import LifecycleHooks
from shared.tasks import RetryPolicy, TaskPolicy, normalize_retry_policy

from lazycloud.references import dotted_reference


@runtime_checkable
class DictExportable(Protocol):
    def to_dict(self) -> Mapping[str, JsonValue]: ...


@runtime_checkable
class ModelDumpable(Protocol):
    def model_dump(self, *, mode: str) -> Mapping[str, JsonValue]: ...


@runtime_checkable
class CallableWrapper(Protocol):
    func: Callable[..., Any]


LifecycleHookReference = str | Callable[..., Any] | CallableWrapper
LifecycleHookInput = LifecycleHookReference | Iterable[LifecycleHookReference] | None
SchemaInput = Mapping[str, JsonValue] | DictExportable | ModelDumpable | None
PoolInput = str | None
"""A scheduling group is a label, so naming one is naming a string.

There is no pool object to pass: the durable row a workload lands on is a
provisioning unit the control plane owns and chooses, and several units may
feed one group.
"""
RetryPolicyInput = RetryPolicy | Mapping[str, JsonValue] | None


def callback_reference(value: LifecycleHookReference | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, CallableWrapper):
        return dotted_reference(value.func)
    if callable(value):
        return dotted_reference(value)
    msg = "callback must be a callable, decorated SDK object, import reference, or None"
    raise TypeError(msg)


def lifecycle_hook_references(value: LifecycleHookInput = None) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (callback_reference(value),)
    if callable(value) or isinstance(value, CallableWrapper):
        return (callback_reference(value),)
    refs = tuple(callback_reference(item) for item in value)
    return tuple(ref for ref in refs if ref)


def lifecycle_hooks(
    *,
    on_start: LifecycleHookInput = None,
    on_running: LifecycleHookInput = None,
    on_success: LifecycleHookInput = None,
    on_error: LifecycleHookInput = None,
    on_retry: LifecycleHookInput = None,
    on_failure: LifecycleHookInput = None,
    on_cancelled: LifecycleHookInput = None,
    on_timeout: LifecycleHookInput = None,
    on_finish: LifecycleHookInput = None,
) -> LifecycleHooks:
    return LifecycleHooks(
        on_start=lifecycle_hook_references(on_start),
        on_running=lifecycle_hook_references(on_running),
        on_success=lifecycle_hook_references(on_success),
        on_error=lifecycle_hook_references(on_error),
        on_retry=lifecycle_hook_references(on_retry),
        on_failure=lifecycle_hook_references(on_failure),
        on_cancelled=lifecycle_hook_references(on_cancelled),
        on_timeout=lifecycle_hook_references(on_timeout),
        on_finish=lifecycle_hook_references(on_finish),
    )


def schema_metadata(value: SchemaInput) -> dict[str, JsonValue]:
    if value is None:
        return {}
    if isinstance(value, DictExportable):
        raw = value.to_dict()
    elif isinstance(value, ModelDumpable):
        raw = value.model_dump(mode="json")
    else:
        raw = value
    return _json_object(raw, field="schema")


def task_policy_metadata(
    value: TaskPolicy | Mapping[str, JsonValue] | None,
) -> dict[str, JsonValue]:
    if value is None:
        return {}
    raw = value.model_dump(mode="json") if isinstance(value, ModelDumpable) else value
    return _json_object(raw, field="task_policy")


def retry_policy_config(
    value: RetryPolicyInput = None,
    *,
    retries: int | None = None,
    retry_delay_seconds: float | None = None,
    retry_for_refs: Iterable[str] | None = None,
) -> RetryPolicy | None:
    if (
        value is None
        and (retries is None or retries <= 0)
        and (retry_delay_seconds is None or retry_delay_seconds <= 0)
        and not retry_for_refs
    ):
        return None
    return normalize_retry_policy(
        value,
        retries=retries,
        delay_seconds=retry_delay_seconds,
        retry_for=retry_for_refs,
    )


def autoscaler_metadata(
    value: QueueDepthAutoscaler | Mapping[str, JsonValue] | None,
) -> dict[str, JsonValue]:
    if value is None:
        return {}
    raw = value.model_dump(mode="json") if isinstance(value, ModelDumpable) else value
    return QueueDepthAutoscaler.model_validate(raw).model_dump(mode="json")


def pool_metadata(value: PoolInput, *, provider: str | None = None) -> dict[str, JsonValue]:
    payload: dict[str, JsonValue] = {} if value is None else {"name": value}
    if provider:
        payload["provider"] = provider
    return payload


def build_resource_metadata(
    *,
    app: str | None = None,
    workers: int | None = None,
    max_pending_tasks: int | None = None,
    retries: int | None = None,
    callback_url: str | None = None,
    authorized: bool | None = None,
    autoscaler: QueueDepthAutoscaler | Mapping[str, JsonValue] | None = None,
    task_policy: TaskPolicy | Mapping[str, JsonValue] | None = None,
    checkpoint_enabled: bool | None = None,
    inputs: SchemaInput = None,
    outputs: SchemaInput = None,
    tcp: bool | None = None,
    block_network: bool | None = None,
    allow_list: list[str] | None = None,
    docker_enabled: bool | None = None,
    pool: PoolInput = None,
    provider: str | None = None,
    extra: Mapping[str, JsonValue] | None = None,
) -> dict[str, JsonValue]:
    metadata: dict[str, JsonValue] = dict(extra or {})
    _set_if_value(metadata, "app", app)
    _set_if_value(metadata, "workers", workers)
    _set_if_value(metadata, "max_pending_tasks", max_pending_tasks)
    _set_if_value(metadata, "callback_url", callback_url)
    _set_if_value(metadata, "authorized", authorized)
    _set_if_value(metadata, "checkpoint_enabled", checkpoint_enabled)
    _set_if_value(metadata, "tcp", tcp)
    _set_if_value(metadata, "block_network", block_network)
    _set_if_value(metadata, "docker_enabled", docker_enabled)
    if allow_list is not None:
        metadata["allow_list"] = list(allow_list)
    if autoscaler is not None:
        metadata["autoscaler"] = autoscaler_metadata(autoscaler)
    if task_policy is not None:
        metadata["task_policy"] = task_policy_metadata(task_policy)
    if inputs is not None:
        metadata["inputs"] = schema_metadata(inputs)
    if outputs is not None:
        metadata["outputs"] = schema_metadata(outputs)
    if pool is not None or provider:
        metadata["pool"] = pool_metadata(pool, provider=provider)
    return metadata


def _set_if_value(metadata: dict[str, JsonValue], key: str, value: JsonValue) -> None:
    if value is not None and value != "":
        metadata[key] = value


def _json_object(value: Mapping[str, JsonValue], *, field: str) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            msg = f"{field} keys must be strings"
            raise TypeError(msg)
        result[key] = _json_value(item, field=field)
    return result


def _json_value(value: JsonValue, *, field: str) -> JsonValue:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        return _json_object(value, field=field)
    if isinstance(value, list | tuple | set):
        return [_json_value(item, field=field) for item in value]
    if isinstance(value, Enum) and isinstance(value.value, str | int | float):
        return value.value
    msg = f"{field} value is not JSON serializable: {value!r}"
    raise TypeError(msg)


__all__ = [
    "CallableWrapper",
    "LifecycleHookInput",
    "LifecycleHookReference",
    "PoolInput",
    "RetryPolicyInput",
    "SchemaInput",
    "autoscaler_metadata",
    "build_resource_metadata",
    "callback_reference",
    "lifecycle_hook_references",
    "lifecycle_hooks",
    "pool_metadata",
    "retry_policy_config",
    "schema_metadata",
    "task_policy_metadata",
]
