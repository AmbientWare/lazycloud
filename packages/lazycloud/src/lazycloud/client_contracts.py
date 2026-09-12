from __future__ import annotations

import dataclasses
import inspect
from collections.abc import Callable, Mapping
from typing import Any, get_type_hints

from pydantic import JsonValue, TypeAdapter
from shared.deployments import DeploymentKind
from shared.http.client_manifests import (
    ClientContract,
    ClientOperation,
    ClientOperationName,
    ClientParameter,
)
from shared.schema import ValueSchema
from shared.serialization import to_json_value

from lazycloud.abstractions.metadata import SchemaInput, schema_metadata
from lazycloud.json_contracts import validate_json_object

_MISSING = object()


class ClientContractError(ValueError):
    """Raised when a callable annotation cannot be exported as a client contract."""


def build_client_contract(
    func: Callable[..., Any],
    *,
    kind: DeploymentKind,
    inputs: SchemaInput = None,
    outputs: SchemaInput = None,
) -> ClientContract | None:
    operation = _operation_name(kind)
    if operation is None:
        return None

    hints = _type_hints(func)
    signature = inspect.signature(func)
    parameters = (
        _parameters_from_schema(schema_metadata(inputs), signature=signature)
        if inputs is not None
        else [
            _parameter_from_signature(name, parameter, hints)
            for name, parameter in signature.parameters.items()
            if name not in {"self", "cls"}
        ]
    )
    return_schema = _return_schema(func, hints=hints, outputs=outputs)
    return ClientContract(
        operation=ClientOperation(
            name=operation,
            parameters=parameters,
            return_schema=return_schema,
        )
    )


def schema_from_contract_parameters(
    contract: ClientContract | None,
) -> dict[str, JsonValue]:
    if contract is None:
        return {}
    return {
        "fields": {
            parameter.name: _metadata_from_json_schema(parameter.json_schema)
            for parameter in contract.operation.parameters
            if parameter.parameter_kind not in {"var_positional", "var_keyword"}
        }
    }


def schema_from_contract_return(contract: ClientContract | None) -> dict[str, JsonValue]:
    if contract is None or not contract.operation.return_schema:
        return {}
    return {
        "fields": {
            "return": _metadata_from_json_schema(contract.operation.return_schema),
        }
    }


def asgi_client_contract() -> ClientContract:
    return ClientContract(
        operation=ClientOperation(
            name=ClientOperationName.Request,
            parameters=[
                _client_parameter(
                    "method",
                    {"type": "string"},
                    required=False,
                    default="POST",
                    default_repr='"POST"',
                    parameter_kind="keyword_only",
                ),
                _client_parameter(
                    "path",
                    {"type": "string"},
                    required=False,
                    default="",
                    default_repr='""',
                    parameter_kind="keyword_only",
                ),
                _client_parameter(
                    "json",
                    {"anyOf": [{}, {"type": "null"}]},
                    required=False,
                    default_repr="None",
                    parameter_kind="keyword_only",
                ),
                _client_parameter(
                    "data",
                    {
                        "anyOf": [
                            {"type": "string", "format": "binary"},
                            {"type": "string"},
                            {"type": "null"},
                        ]
                    },
                    required=False,
                    default_repr="None",
                    parameter_kind="keyword_only",
                ),
                _client_parameter(
                    "headers",
                    {
                        "anyOf": [
                            {
                                "type": "object",
                                "additionalProperties": {"type": "string"},
                            },
                            {"type": "null"},
                        ]
                    },
                    required=False,
                    default_repr="None",
                    parameter_kind="keyword_only",
                ),
                _client_parameter(
                    "params",
                    {
                        "anyOf": [
                            {"type": "object"},
                            {
                                "type": "array",
                                "items": {
                                    "type": "array",
                                    "prefixItems": [{"type": "string"}, {}],
                                    "minItems": 2,
                                    "maxItems": 2,
                                },
                            },
                            {"type": "null"},
                        ]
                    },
                    required=False,
                    default_repr="None",
                    parameter_kind="keyword_only",
                ),
            ],
            return_schema={},
        )
    )


def _operation_name(kind: DeploymentKind) -> ClientOperationName | None:
    if kind is DeploymentKind.Function:
        return ClientOperationName.Remote
    if kind in {DeploymentKind.Endpoint, DeploymentKind.Asgi}:
        return ClientOperationName.Request
    return None


def _type_hints(func: Callable[..., Any]) -> dict[str, Any]:
    try:
        return get_type_hints(func, include_extras=True)
    except Exception as exc:
        annotations = getattr(func, "__annotations__", {})
        if not annotations:
            return {}
        name = getattr(func, "__qualname__", getattr(func, "__name__", "callable"))
        raise ClientContractError(
            f"could not resolve type annotations for {name}; "
            "client-exported callables must use importable annotations"
        ) from exc


def _parameter_from_signature(
    name: str,
    parameter: inspect.Parameter,
    hints: Mapping[str, Any],
) -> ClientParameter:
    default = parameter.default
    default_value, default_repr = _default_payload(default)
    return _client_parameter(
        name,
        _json_schema_for_annotation(hints.get(name, Any)),
        required=default is inspect.Parameter.empty,
        default=default_value,
        default_repr=default_repr,
        parameter_kind=_parameter_kind(parameter),
    )


def _parameter_kind(parameter: inspect.Parameter) -> str:
    if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
        return "var_positional"
    if parameter.kind is inspect.Parameter.VAR_KEYWORD:
        return "var_keyword"
    if parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
        return "positional_only"
    if parameter.kind is inspect.Parameter.KEYWORD_ONLY:
        return "keyword_only"
    return "keyword"


def _parameters_from_schema(
    schema: dict[str, JsonValue], *, signature: inspect.Signature
) -> list[ClientParameter]:
    fields = ValueSchema.from_definition(schema).fields
    parameters: list[ClientParameter] = []
    for name, field in fields.items():
        parameter = signature.parameters.get(name)
        default = inspect.Signature.empty if parameter is None else parameter.default
        default_value, default_repr = _default_payload(default)
        parameters.append(
            _client_parameter(
                name,
                field.input_json_schema(),
                required=default is inspect.Signature.empty,
                default=default_value,
                default_repr=default_repr,
                parameter_kind="keyword" if parameter is None else _parameter_kind(parameter),
            )
        )
    return parameters


def _return_schema(
    func: Callable[..., Any],
    *,
    hints: Mapping[str, Any],
    outputs: SchemaInput,
) -> dict[str, JsonValue]:
    if outputs is not None:
        return _return_schema_from_explicit_schema(schema_metadata(outputs))
    if "return" in hints:
        return _json_schema_for_annotation(hints["return"])
    signature = inspect.signature(func)
    if signature.return_annotation is not inspect.Signature.empty:
        return _json_schema_for_annotation(signature.return_annotation)
    return _json_schema_for_annotation(Any)


def _return_schema_from_explicit_schema(
    schema: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    return ValueSchema.from_definition(schema).output_json_schema()


def _client_parameter(
    name: str,
    json_schema: dict[str, JsonValue],
    *,
    required: bool,
    default: JsonValue = None,
    default_repr: str = "",
    parameter_kind: str = "keyword",
) -> ClientParameter:
    return ClientParameter(
        name=name,
        json_schema=json_schema,
        required=required,
        default=default,
        default_repr=default_repr,
        parameter_kind=parameter_kind,
    )


def _json_schema_for_annotation(annotation: Any) -> dict[str, JsonValue]:
    if (
        annotation is inspect.Parameter.empty
        or annotation is inspect.Signature.empty
        or annotation is Any
    ):
        return {}
    try:
        schema = TypeAdapter(annotation).json_schema(ref_template="#/$defs/{model}")
    except Exception as exc:
        raise ClientContractError(
            f"could not export annotation {annotation!r} as JSON Schema"
        ) from exc
    return validate_json_object(schema)


def _metadata_from_json_schema(schema: dict[str, Any]) -> JsonValue:
    resolved = _non_null_schema(schema)
    schema_type = resolved.get("type") if isinstance(resolved, dict) else None
    if resolved.get("format") == "binary":
        return {"type": "file"}
    if isinstance(schema_type, str) and schema_type in {
        "string",
        "integer",
        "number",
        "boolean",
        "array",
    }:
        return {"type": schema_type}
    if schema_type == "object" or "properties" in resolved:
        return {"type": "object"}
    return {"type": "json"}


def _non_null_schema(schema: dict[str, Any]) -> dict[str, JsonValue]:
    validated = validate_json_object(schema)
    for key in ("anyOf", "oneOf"):
        options = validated.get(key)
        if not isinstance(options, list):
            continue
        non_null = [
            item for item in options if isinstance(item, dict) and item.get("type") != "null"
        ]
        if len(non_null) == 1:
            return non_null[0]
    return validated


def _default_payload(value: object) -> tuple[JsonValue, str]:
    if value is inspect.Parameter.empty or value is _MISSING or value is dataclasses.MISSING:
        return None, ""
    try:
        validated = to_json_value(value)
    except (TypeError, ValueError) as exc:
        raise ClientContractError("default value is not JSON serializable") from exc
    return validated, repr(validated)


__all__ = [
    "ClientContractError",
    "asgi_client_contract",
    "build_client_contract",
    "schema_from_contract_parameters",
    "schema_from_contract_return",
]
