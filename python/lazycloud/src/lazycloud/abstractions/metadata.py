from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from pydantic import JsonValue

from lazycloud._shared.callbacks import normalize_callback_url
from lazycloud._shared.gpu import gpu_preference
from lazycloud._shared.lifecycle import LifecycleHookName
from lazycloud._shared.tasks import RetryPolicy
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
MachineInput = str | None
"""A joined machine this workload must run on, by name; unset runs in the workspace."""
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


def lifecycle_hooks(owner: Any) -> dict[str, list[str]]:
    """The API lifecycle hooks of a workload: each hook it sets, as import references."""
    hooks: dict[str, list[str]] = {}
    for hook in LifecycleHookName:
        refs = lifecycle_hook_references(getattr(owner, hook.value, None))
        if refs:
            hooks[hook.value] = list(refs)
    return hooks


REGIONS = ("us-east", "us-west", "eu-central", "eu-north", "ap-southeast")


def check_workload_options(owner: Any) -> None:
    """Refuse GPU, placement and callback options no deploy could honor, where they are declared."""
    gpu_preference(owner.gpu)
    normalize_callback_url(getattr(owner, "callback_url", None))
    if owner.gpu_count < 0:
        raise ValueError("gpu_count cannot be negative")
    if owner.region is not None and owner.region not in REGIONS:
        raise ValueError(f"region {owner.region!r} is not one of {', '.join(REGIONS)}")
    if (owner.region is not None or owner.availability_zone) and owner.machine:
        raise ValueError(
            "region or availability zone and a named machine cannot be selected together"
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


def retry_policy_config(
    value: RetryPolicyInput = None,
    *,
    retries: int | None = None,
    retry_delay_seconds: float | None = None,
    default_retries: int = 0,
) -> RetryPolicy | None:
    """Resolve a workload's retries; `retries` and the delay override a policy's fields.

    `default_retries` applies only when the caller set none of the three.
    """
    if value is None and retries is None and retry_delay_seconds is None:
        retries = default_retries
    if (
        value is None
        and (retries is None or retries <= 0)
        and (retry_delay_seconds is None or retry_delay_seconds <= 0)
    ):
        return None
    fields = RetryPolicy.model_validate(value or {}).model_dump()
    if retries is not None:
        fields["max_attempts"] = max(int(retries), 0) + 1
    if retry_delay_seconds is not None:
        fields["delay_seconds"] = max(float(retry_delay_seconds), 0.0)
    return RetryPolicy.model_validate(fields)


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
    "REGIONS",
    "CallableWrapper",
    "LifecycleHookInput",
    "LifecycleHookReference",
    "MachineInput",
    "RetryPolicyInput",
    "SchemaInput",
    "callback_reference",
    "check_workload_options",
    "lifecycle_hook_references",
    "lifecycle_hooks",
    "retry_policy_config",
    "schema_metadata",
]
