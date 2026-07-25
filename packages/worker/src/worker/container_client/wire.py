from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from enum import StrEnum

from pydantic import JsonValue
from shared.bytes_transport import decode_bytes, encode_bytes
from shared.contracts import ContractModel

from worker.container_client.models import ContainerServiceWireValue

CONTAINER_SERVICE_HTTP_PREFIX = "/container-service"
_BYTES_MARKER = "__bytes_base64__"

type ContainerServiceWireSource = (
    ContainerServiceWireValue
    | Mapping[str, ContainerServiceWireValue]
    | ContractModel
    | StrEnum
    | date
    | tuple[ContainerServiceWireSource, ...]
    | set[ContainerServiceWireSource]
)


def encode_container_service_wire_value(
    value: ContainerServiceWireSource,
) -> ContainerServiceWireValue:
    if isinstance(value, bytes):
        return {_BYTES_MARKER: encode_bytes(value)}
    if isinstance(value, ContractModel):
        return encode_container_service_wire_value(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        return {str(key): encode_container_service_wire_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [encode_container_service_wire_value(item) for item in value]
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


def decode_container_service_wire_value(value: JsonValue) -> ContainerServiceWireValue:
    if isinstance(value, dict):
        if set(value) == {_BYTES_MARKER}:
            encoded = value[_BYTES_MARKER]
            if not isinstance(encoded, str):
                raise ValueError("container service bytes marker must contain a string")
            return decode_bytes(encoded)
        return {key: decode_container_service_wire_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [decode_container_service_wire_value(item) for item in value]
    return value
