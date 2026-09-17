from __future__ import annotations

from pydantic import Field, JsonValue, field_validator, model_validator

from shared.app_slug import validate_app_slug
from shared.deployments import DeploymentKind
from shared.enums import StringEnum
from shared.http.base import HttpModel


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


INVOKABLE_DEPLOYMENT_KINDS = frozenset(
    {
        DeploymentKind.Function,
        DeploymentKind.Endpoint,
        DeploymentKind.Asgi,
    }
)


def client_manifest_schemas(
    metadata: dict[str, JsonValue],
) -> tuple[dict[str, JsonValue], dict[str, JsonValue]]:
    """Extract the input/output schema dicts recorded on a deployment spec."""
    schema = metadata.get("schema")
    schema_mapping = schema if isinstance(schema, dict) else {}
    inputs = schema_mapping.get("inputs")
    outputs = schema_mapping.get("outputs")
    if not isinstance(inputs, dict):
        inputs = metadata.get("inputs")
    if not isinstance(outputs, dict):
        outputs = metadata.get("outputs")
    return (
        inputs if isinstance(inputs, dict) else {},
        outputs if isinstance(outputs, dict) else {},
    )


class ClientManifestResource(HttpModel):
    app: str
    name: str
    kind: DeploymentKind
    stub_id: str
    deployment_id: str
    deployment_version: int
    invoke_url: str
    """The hostname this resource answers on, for publishing and sharing."""

    invoke_path: str
    """Same-origin path the platform serves it at, for a caller the platform serves.

    A hostname invoke is cross-origin to the dashboard, and a deployed resource owes
    the dashboard no CORS permission. Distinct from `invoke_url` on purpose: one is
    what a user publishes, the other is how a first-party client reaches it.
    """

    route: str | None = None
    timeout_seconds: int | None = Field(default=None, ge=0)
    methods: list[str] = Field(default_factory=list)
    inputs: dict[str, JsonValue] = Field(default_factory=dict)
    outputs: dict[str, JsonValue] = Field(default_factory=dict)
    client_contract: ClientContract | None = None

    @field_validator("app")
    @classmethod
    def app_must_be_slug(cls, value: str) -> str:
        return validate_app_slug(value)


class ClientManifestRequest(HttpModel):
    app: str
    workspace: str = "default"
    external_url: str = "http://127.0.0.1:9000"

    @field_validator("app")
    @classmethod
    def app_must_be_slug(cls, value: str) -> str:
        return validate_app_slug(value)


class ClientManifestResponse(HttpModel):
    app: str = ""
    workspace: str = "default"
    resources: list[ClientManifestResource] = Field(default_factory=list)


__all__ = [
    "INVOKABLE_DEPLOYMENT_KINDS",
    "ClientContract",
    "ClientManifestRequest",
    "ClientManifestResource",
    "ClientManifestResponse",
    "ClientOperation",
    "ClientOperationName",
    "ClientParameter",
    "client_manifest_schemas",
]
