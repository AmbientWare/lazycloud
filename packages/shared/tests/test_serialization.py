from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import BaseModel, JsonValue, SecretBytes, SecretStr, TypeAdapter, field_serializer
from shared.serialization import to_json_value

_JSON_VALUE = TypeAdapter[JsonValue](JsonValue)


class _SecretSerializerPayload(BaseModel):
    credential: SecretStr

    @field_serializer("credential")
    def serialize_credential(self, value: SecretStr) -> str:
        return value.get_secret_value()


class _IntegerKeyPayload(BaseModel):
    attributes: dict[int, str]


class _SetPayload(BaseModel):
    values: set[str]


class _UnsupportedValue:
    def __repr__(self) -> str:
        raise AssertionError("unsupported values must not reach repr")


@pytest.mark.parametrize(
    "case",
    ["numeric", "set", "keys", "pydantic-set", "secret", "serializer", "unsupported", "recursive"],
)
def test_to_json_value_rejects_unsafe_or_unsupported_values(case: str) -> None:
    if case == "numeric":
        for value in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(ValueError, match="non-finite"):
                to_json_value({"value": value})
    elif case == "set":
        for value in ({"item"}, frozenset({"item"})):
            with pytest.raises(ValueError, match="set values"):
                to_json_value(value)
    elif case == "keys":
        with pytest.raises(ValueError, match="keys must be strings"):
            to_json_value({1: "value"})
        with pytest.raises(ValueError, match="keys must be strings"):
            to_json_value(_IntegerKeyPayload(attributes={1: "value"}))
    elif case == "pydantic-set":
        with pytest.raises(ValueError, match="set values"):
            to_json_value(_SetPayload(values={"value"}))
    elif case == "secret":
        secret_text = SecretStr(uuid4().hex)
        secret_bytes = SecretBytes(uuid4().bytes)
        for value in (secret_text, secret_bytes, {"nested": [secret_text]}):
            with pytest.raises(ValueError, match="secret-bearing"):
                to_json_value(value)
    elif case == "serializer":
        payload = _SecretSerializerPayload(credential=SecretStr(uuid4().hex))
        with pytest.raises(ValueError, match="secret-bearing"):
            to_json_value(payload)
    elif case == "unsupported":
        with pytest.raises(TypeError, match="unsupported JSON value type"):
            to_json_value(_UnsupportedValue())
    else:
        recursive: list[object] = []
        recursive.append(recursive)
        with pytest.raises(ValueError, match="recursive values"):
            to_json_value(recursive)
