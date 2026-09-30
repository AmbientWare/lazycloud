from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from database.records.apps import StubRecord
from database.repositories.disks import DiskRepository
from database.types import DatabaseSession
from pydantic import JsonValue, TypeAdapter
from shared.contracts import ContractModel
from shared.disks import disk_shrink_message
from shared.errors import InvalidInputError
from shared.workload_config import StubConfig

_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])
_JSON_VALUE_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
type StubConfigUpdateValue = JsonValue | ContractModel


def _stub_config_payload(config: StubConfig) -> dict[str, JsonValue]:
    return _JSON_OBJECT_ADAPTER.validate_python(
        config.model_dump(mode="json", exclude_unset=True, by_alias=True)
    )


def _assert_disks_keep_their_size(
    session: DatabaseSession, config: StubConfig, *, workspace_id: str
) -> None:
    """Disk growth happens on acquisition; shrinking is never supported."""
    recorded = DiskRepository(session).declared_sizes(
        [mount.name for mount in config.disks], workspace_id=workspace_id
    )
    for mount in config.disks:
        size = recorded.get(mount.name)
        if size is not None and mount.size_bytes < size:
            raise InvalidInputError(disk_shrink_message(mount.name, size))


def _stub_preparation_fingerprint(stub: StubRecord) -> str:
    payload = stub.model_dump(mode="json", exclude={"id", "created_at", "updated_at"})
    config = _stub_config_payload(stub.config)
    if not config.get("object_id"):
        config.pop("object_id", None)
    for section in ("runtime", "autoscaler", "task_policy", "image"):
        values = config.get(section)
        if isinstance(values, dict):
            config[section] = {key: value for key, value in values.items() if value is not None}
    payload["config"] = config
    encoded = json.dumps(
        _fingerprint_value(payload), sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def _fingerprint_value(value: JsonValue) -> JsonValue:
    # SQL floating-point columns preserve the value, but not JSON's integer spelling.
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {key: _fingerprint_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_fingerprint_value(item) for item in value]
    return value


def _masked_config(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        masked: dict[str, JsonValue] = {}
        for key, item in value.items():
            lowered = key.lower()
            if any(token in lowered for token in ("secret", "token", "password", "key")):
                masked[key] = "********"
            else:
                masked[key] = _masked_config(item)
        return masked
    if isinstance(value, list):
        return [_masked_config(item) for item in value]
    return value


def _masked_config_object(config: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    return {key: _masked_config(value) for key, value in config.items()}


def _limited_public_config(config: StubConfig) -> dict[str, JsonValue]:
    payload = _stub_config_payload(config)
    return {
        key: _masked_config(payload[key])
        for key in ("inputs", "outputs", "task_policy", "python_version", "runtime")
        if key in payload
    }


def _set_nested_config_value(
    config: dict[str, JsonValue], path: str, value: StubConfigUpdateValue
) -> None:
    parts = path.split(".")
    if not parts or any(not part for part in parts):
        raise InvalidInputError("config field path cannot be empty")
    current: dict[str, JsonValue] = config
    for part in parts[:-1]:
        existing = current.get(part)
        if existing is None:
            nested: dict[str, JsonValue] = {}
            current[part] = nested
            current = nested
            continue
        if not isinstance(existing, dict):
            raise InvalidInputError(f"config field is not an object: {part}")
        current = existing
    current[parts[-1]] = (
        _JSON_VALUE_ADAPTER.validate_python(value.model_dump(mode="json"))
        if isinstance(value, ContractModel)
        else value
    )
