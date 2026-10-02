from __future__ import annotations

from pydantic import Field, JsonValue, model_validator

from lazycloud._shared.enums import StringEnum
from lazycloud._shared.http.base import HttpModel


class ClientOperationName(StringEnum):
    Remote = "remote"
    Request = "request"
    Put = "put"


class ClientParameter(HttpModel):
    name: str
    json_schema: dict[str, JsonValue] | None = Field(default_factory=dict)
    python_type: str = ""
    required: bool = True
    default: JsonValue = None
    default_repr: str = ""
    python_default: bool = False
    parameter_kind: str = "keyword"

    @model_validator(mode="after")
    def validate_python_contract(self) -> ClientParameter:
        if (self.json_schema is None) != bool(self.python_type):
            raise ValueError("a Python-only parameter must name its type and omit its JSON schema")
        if self.python_default and (self.required or self.default is not None or self.default_repr):
            raise ValueError("Python defaults remain in the handler and cannot be exported")
        return self


class ClientOperation(HttpModel):
    name: ClientOperationName
    parameters: list[ClientParameter] = Field(default_factory=list)
    return_schema: dict[str, JsonValue] | None = Field(default_factory=dict)
    return_python_type: str = ""

    @model_validator(mode="after")
    def validate_python_contract(self) -> ClientOperation:
        if (self.return_schema is None) != bool(self.return_python_type):
            raise ValueError("a Python-only return must name its type and omit its JSON schema")
        return self


class ClientContract(HttpModel):
    operation: ClientOperation

    def json_export_errors(self) -> list[str]:
        errors: list[str] = []
        for parameter in self.operation.parameters:
            if parameter.python_type:
                errors.append(f"parameter {parameter.name}: {parameter.python_type}")
            if parameter.python_default:
                errors.append(f"parameter {parameter.name} has a Python default")
        if self.operation.return_python_type:
            errors.append(f"return: {self.operation.return_python_type}")
        return errors


__all__ = [
    "ClientContract",
    "ClientOperation",
    "ClientOperationName",
    "ClientParameter",
]
