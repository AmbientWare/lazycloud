from __future__ import annotations

import json
from collections.abc import Iterable, Mapping

from pydantic import JsonValue, TypeAdapter
from shared.contracts import ContractModel

from coordination.redis_client import RedisWireScalar, redis_text

_JSON_VALUE_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_JSON_MAPPING_ADAPTER: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


def dump_model_hash(model: ContractModel) -> dict[str, str]:
    payload = _JSON_MAPPING_ADAPTER.validate_json(model.model_dump_json())
    return {
        key: json.dumps(value, separators=(",", ":"), sort_keys=True)
        for key, value in payload.items()
    }


def load_model_hash[T: ContractModel](
    model_type: type[T],
    values: Mapping[RedisWireScalar, RedisWireScalar],
) -> T:
    payload = {redis_text(key): loads_field(value) for key, value in values.items()}
    return model_type.model_validate(payload)


def dump_model_json(model: ContractModel) -> str:
    return model.model_dump_json()


def load_model_json[T: ContractModel](model_type: type[T], value: RedisWireScalar) -> T:
    return model_type.model_validate_json(redis_text(value))


def loads_field(value: RedisWireScalar) -> JsonValue:
    if isinstance(value, bytes):
        value = value.decode()
    if not isinstance(value, str):
        return value
    return _JSON_VALUE_ADAPTER.validate_json(value)


def redis_strings(values: Iterable[RedisWireScalar]) -> list[str]:
    return [redis_text(value) for value in values]
