from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal, TypeAlias

from pydantic import Field, JsonValue, model_validator

from shared.bytes_transport import EncodedBytesBody, encode_bytes
from shared.contracts import ContractModel
from shared.enums import StringEnum

FUNCTION_PAYLOAD_VERSION = 1
FUNCTION_PAYLOAD_MAX_BYTES = 16 * 1024 * 1024
FUNCTION_DEPENDENCY_MAX_COUNT = 256
FUNCTION_BOUND_RESULTS_MAX_BYTES = 64 * 1024 * 1024
FUNCTION_MARKER_MAX_DEPTH = 128
FUNCTION_MARKER_MAX_NODES = 100_000
FUNCTION_PAYLOAD_BASE64_MAX_CHARS = ((FUNCTION_PAYLOAD_MAX_BYTES + 2) // 3) * 4


class FunctionPayloadEncoding(StringEnum):
    Json = "json"
    Cloudpickle = "cloudpickle"


class FunctionJsonInvocation(ContractModel):
    version: Literal[1] = FUNCTION_PAYLOAD_VERSION
    encoding: Literal[FunctionPayloadEncoding.Json] = FunctionPayloadEncoding.Json
    args: list[JsonValue] = Field(default_factory=list)
    kwargs: dict[str, JsonValue] = Field(default_factory=dict)
    result_encoding: Literal[FunctionPayloadEncoding.Json] = FunctionPayloadEncoding.Json

    @model_validator(mode="after")
    def validate_size(self) -> FunctionJsonInvocation:
        _validate_json_size(
            {
                "args": self.args,
                "kwargs": self.kwargs,
                "result_encoding": self.result_encoding.value,
            },
            kind="invocation",
        )
        return self


class FunctionInvocationArguments(ContractModel):
    args: list[JsonValue] = Field(default_factory=list)
    kwargs: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_size(self) -> FunctionInvocationArguments:
        _validate_json_size(
            {
                "args": self.args,
                "kwargs": self.kwargs,
            },
            kind="invocation arguments",
        )
        return self


class FunctionCloudpickleInvocation(EncodedBytesBody):
    version: Literal[1] = FUNCTION_PAYLOAD_VERSION
    encoding: Literal[FunctionPayloadEncoding.Cloudpickle] = FunctionPayloadEncoding.Cloudpickle
    value_base64: str = Field(default="", max_length=FUNCTION_PAYLOAD_BASE64_MAX_CHARS)
    size_bytes: int = Field(ge=0, le=FUNCTION_PAYLOAD_MAX_BYTES)
    sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    arguments: FunctionInvocationArguments | None = None

    @classmethod
    def from_bytes(
        cls,
        value: bytes,
        *,
        arguments: FunctionInvocationArguments | None = None,
    ) -> FunctionCloudpickleInvocation:
        _validate_binary_size(value, kind="invocation")
        return cls(
            value_base64=encode_bytes(value),
            size_bytes=len(value),
            sha256=hashlib.sha256(value).hexdigest(),
            arguments=arguments,
        )

    @model_validator(mode="after")
    def validate_content(self) -> FunctionCloudpickleInvocation:
        _validate_binary_payload(
            self.bytes_value(),
            declared_size=self.size_bytes,
            declared_sha256=self.sha256,
            kind="invocation",
        )
        return self


FunctionInvocationPayload: TypeAlias = Annotated[
    FunctionJsonInvocation | FunctionCloudpickleInvocation,
    Field(discriminator="encoding"),
]


class FunctionJsonResult(ContractModel):
    version: Literal[1] = FUNCTION_PAYLOAD_VERSION
    encoding: Literal[FunctionPayloadEncoding.Json] = FunctionPayloadEncoding.Json
    value: JsonValue = None

    @model_validator(mode="after")
    def validate_size(self) -> FunctionJsonResult:
        _validate_json_size(self.value, kind="result")
        return self


class FunctionCloudpickleResult(EncodedBytesBody):
    version: Literal[1] = FUNCTION_PAYLOAD_VERSION
    encoding: Literal[FunctionPayloadEncoding.Cloudpickle] = FunctionPayloadEncoding.Cloudpickle
    value_base64: str = Field(default="", max_length=FUNCTION_PAYLOAD_BASE64_MAX_CHARS)
    size_bytes: int = Field(ge=0, le=FUNCTION_PAYLOAD_MAX_BYTES)
    sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def from_bytes(cls, value: bytes) -> FunctionCloudpickleResult:
        _validate_binary_size(value, kind="result")
        return cls(
            value_base64=encode_bytes(value),
            size_bytes=len(value),
            sha256=hashlib.sha256(value).hexdigest(),
        )

    @model_validator(mode="after")
    def validate_content(self) -> FunctionCloudpickleResult:
        _validate_binary_payload(
            self.bytes_value(),
            declared_size=self.size_bytes,
            declared_sha256=self.sha256,
            kind="result",
        )
        return self


FunctionResultPayload: TypeAlias = Annotated[
    FunctionJsonResult | FunctionCloudpickleResult,
    Field(discriminator="encoding"),
]


class FunctionDependencyBinding(ContractModel):
    task_id: str
    result: FunctionResultPayload


def function_result_payload_size(result: FunctionResultPayload) -> int:
    if result.encoding is FunctionPayloadEncoding.Cloudpickle:
        return result.size_bytes
    return len(_json_bytes(result.value))


def validate_function_dependency_bindings(
    bindings: list[FunctionDependencyBinding],
) -> None:
    if len(bindings) > FUNCTION_DEPENDENCY_MAX_COUNT:
        raise ValueError(f"function dependency count exceeds {FUNCTION_DEPENDENCY_MAX_COUNT}")
    task_ids = [binding.task_id for binding in bindings]
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("function dependency bindings must have unique task ids")
    total_size = sum(function_result_payload_size(binding.result) for binding in bindings)
    if total_size > FUNCTION_BOUND_RESULTS_MAX_BYTES:
        raise ValueError(
            f"bound function dependency results exceed {FUNCTION_BOUND_RESULTS_MAX_BYTES} bytes"
        )


def _validate_json_size(value: JsonValue, *, kind: str) -> None:
    size = len(_json_bytes(value))
    if size > FUNCTION_PAYLOAD_MAX_BYTES:
        raise ValueError(f"function {kind} exceeds {FUNCTION_PAYLOAD_MAX_BYTES} bytes")


def _json_bytes(value: JsonValue) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _validate_binary_size(value: bytes, *, kind: str) -> None:
    if len(value) > FUNCTION_PAYLOAD_MAX_BYTES:
        raise ValueError(f"function {kind} exceeds {FUNCTION_PAYLOAD_MAX_BYTES} bytes")


def _validate_binary_payload(
    value: bytes,
    *,
    declared_size: int,
    declared_sha256: str,
    kind: str,
) -> None:
    _validate_binary_size(value, kind=kind)
    if len(value) != declared_size:
        raise ValueError(f"function {kind} size does not match its payload")
    if hashlib.sha256(value).hexdigest() != declared_sha256:
        raise ValueError(f"function {kind} SHA-256 does not match its payload")


__all__ = [
    "FUNCTION_BOUND_RESULTS_MAX_BYTES",
    "FUNCTION_DEPENDENCY_MAX_COUNT",
    "FUNCTION_MARKER_MAX_DEPTH",
    "FUNCTION_MARKER_MAX_NODES",
    "FUNCTION_PAYLOAD_MAX_BYTES",
    "FUNCTION_PAYLOAD_VERSION",
    "FunctionCloudpickleInvocation",
    "FunctionCloudpickleResult",
    "FunctionDependencyBinding",
    "FunctionInvocationArguments",
    "FunctionInvocationPayload",
    "FunctionJsonInvocation",
    "FunctionJsonResult",
    "FunctionPayloadEncoding",
    "FunctionResultPayload",
    "function_result_payload_size",
    "validate_function_dependency_bindings",
]
