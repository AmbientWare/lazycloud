from __future__ import annotations

import importlib
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

import pytest
from lazycloud.abstractions.endpoint import EndpointResponse
from lazycloud.cli.main import build_public_cli
from lazycloud.client_handles import EndpointHandle
from lazycloud.control import ControlClientConfig
from lazycloud.json_contracts import JsonValue, parse_json_object
from shared.deployments import DeploymentKind
from shared.http.client_manifests import (
    ClientContract,
    ClientManifestRequest,
    ClientManifestResource,
    ClientManifestResponse,
    ClientOperation,
    ClientOperationName,
    ClientParameter,
)
from tests.url_constants import EXAMPLE_COM_URL
from typer.testing import CliRunner

cli = build_public_cli()


@dataclass
class _FakeClientManifestGateway:
    requests: list[ClientManifestRequest] = field(default_factory=list)

    def client_manifest(self, request: ClientManifestRequest) -> ClientManifestResponse:
        self.requests.append(request)
        return ClientManifestResponse(
            app=request.app,
            workspace=request.workspace,
            resources=[
                ClientManifestResource(
                    app=request.app,
                    name="health",
                    kind=DeploymentKind.Endpoint,
                    stub_id="stub-health",
                    deployment_id="dep-health",
                    deployment_version=3,
                    invoke_url=f"{request.external_url}/endpoint/public/stub-health",
                    invoke_path="/api/v1/endpoints/health/latest",
                    route="/health",
                    methods=["GET"],
                    inputs={"fields": {}},
                    outputs={"fields": {"status": {"type": "string"}}},
                    client_contract=_client_contract(
                        ClientOperationName.Request,
                        [
                            ClientParameter(
                                name="user_id",
                                json_schema={"type": "integer"},
                            ),
                            ClientParameter(
                                name="include_details",
                                json_schema={"type": "boolean"},
                                required=False,
                                default=False,
                                default_repr="False",
                            ),
                        ],
                        return_schema={
                            "$defs": {
                                "HealthDetails": {
                                    "title": "HealthDetails",
                                    "type": "object",
                                    "properties": {"code": {"type": "integer"}},
                                    "required": ["code"],
                                }
                            },
                            "title": "HealthResponse",
                            "type": "object",
                            "properties": {
                                "status": {"type": "string"},
                                "details": {"$ref": "#/$defs/HealthDetails"},
                            },
                            "required": ["status", "details"],
                        },
                    ),
                ),
                ClientManifestResource(
                    app=request.app,
                    name="square",
                    kind=DeploymentKind.Function,
                    stub_id="stub-square",
                    deployment_id="dep-square",
                    deployment_version=2,
                    invoke_url=f"{request.external_url}/function/stub-square",
                    invoke_path="/api/v1/functions/square/latest",
                    inputs={"fields": {"value": {"type": "integer"}}},
                    outputs={"fields": {"result": {"type": "integer"}}},
                    client_contract=_client_contract(
                        ClientOperationName.Remote,
                        [
                            ClientParameter(
                                name="value",
                                json_schema={"type": "integer"},
                            )
                        ],
                        return_schema={"type": "integer"},
                    ),
                ),
                ClientManifestResource(
                    app=request.app,
                    name="site",
                    kind=DeploymentKind.Asgi,
                    stub_id="stub-site",
                    deployment_id="dep-site",
                    deployment_version=4,
                    invoke_url=f"{request.external_url}/asgi/stub-site",
                    invoke_path="/api/v1/asgi/site/latest",
                    route="/",
                    methods=["GET", "POST"],
                    client_contract=_client_contract(
                        ClientOperationName.Request,
                        [
                            ClientParameter(
                                name="method",
                                json_schema={"type": "string"},
                                required=False,
                                default="POST",
                                default_repr='"POST"',
                            ),
                            ClientParameter(
                                name="path",
                                json_schema={"type": "string"},
                                required=False,
                                default="",
                                default_repr='""',
                            ),
                        ],
                    ),
                ),
                ClientManifestResource(
                    app=request.app,
                    name="worker",
                    kind=DeploymentKind.Pod,
                    stub_id="stub-worker",
                    deployment_id="dep-worker",
                    deployment_version=1,
                    invoke_url=f"{request.external_url}/pod/id/stub-worker/8080",
                    invoke_path="/pod/id/stub-worker/8080",
                ),
            ],
        )


@dataclass
class _FakeUntypedClientManifestGateway:
    requests: list[ClientManifestRequest] = field(default_factory=list)

    def client_manifest(self, request: ClientManifestRequest) -> ClientManifestResponse:
        self.requests.append(request)
        return ClientManifestResponse(
            app=request.app,
            workspace=request.workspace,
            resources=[
                ClientManifestResource(
                    app=request.app,
                    name="health",
                    kind=DeploymentKind.Endpoint,
                    stub_id="stub-health",
                    deployment_id="dep-health",
                    deployment_version=3,
                    invoke_url=f"{request.external_url}/endpoint/public/stub-health",
                    invoke_path="/api/v1/endpoints/health/latest",
                    route="/health",
                    methods=["GET"],
                    inputs={"fields": {}},
                    outputs={"fields": {"status": {"type": "string"}}},
                    client_contract=None,
                )
            ],
        )


@runtime_checkable
class _GeneratedHealthDetails(Protocol):
    code: int


@runtime_checkable
class _GeneratedHealthResponse(Protocol):
    status: str
    details: _GeneratedHealthDetails


class _GeneratedHealthResponseFactory(Protocol):
    def __call__(
        self,
        *,
        status: str,
        details: dict[str, int],
    ) -> _GeneratedHealthResponse: ...


@runtime_checkable
class _GeneratedHealthHandle(Protocol):
    request: Callable[..., _GeneratedHealthResponse]
    async_request: Callable[..., Awaitable[_GeneratedHealthResponse]]
    HealthResponse: _GeneratedHealthResponseFactory
    HealthDetails: type[_GeneratedHealthDetails]


@runtime_checkable
class _GeneratedSiteHandle(Protocol):
    request: Callable[..., EndpointResponse]
    async_request: Callable[..., Awaitable[EndpointResponse]]


@runtime_checkable
class _GeneratedClientPackage(Protocol):
    __all__: list[str]
    health: _GeneratedHealthHandle
    site: _GeneratedSiteHandle


def _client_contract(
    operation: ClientOperationName,
    parameters: list[ClientParameter],
    *,
    return_schema: dict[str, JsonValue] | None = None,
) -> ClientContract:
    return ClientContract(
        operation=ClientOperation(
            name=operation,
            parameters=parameters,
            return_schema=return_schema or {},
        )
    )


def _json_object(raw: str, name: str) -> dict[str, JsonValue]:
    try:
        return parse_json_object(raw)
    except ValueError as exc:
        raise AssertionError(f"{name} must be a JSON object") from exc


def _json_path(value: JsonValue, *path: str | int) -> JsonValue:
    current = value
    for segment in path:
        if isinstance(segment, str):
            if not isinstance(current, dict):
                raise AssertionError(f"expected JSON object before {segment!r}")
            current = current[segment]
        else:
            if not isinstance(current, list):
                raise AssertionError(f"expected JSON array before index {segment}")
            current = current[segment]
    return current


def _json_string(value: JsonValue, *path: str | int) -> str:
    selected = _json_path(value, *path)
    if not isinstance(selected, str):
        raise AssertionError(f"expected JSON string at {path!r}")
    return selected


def test_public_cli_generated_client_returns_typed_endpoint_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    gateway = _FakeClientManifestGateway()

    def control_config(workspace: str | None) -> ControlClientConfig:
        return ControlClientConfig(
            endpoint=EXAMPLE_COM_URL,
            token="token",
            workspace=workspace or "default",
            timeout_seconds=10,
        )

    def manifest_gateway(
        endpoint: str,
        *,
        token: str | None,
        timeout_seconds: float,
    ) -> _FakeClientManifestGateway:
        _ = endpoint, token, timeout_seconds
        return gateway

    monkeypatch.setattr(
        "lazycloud.client_codegen.resolve_control_client_config",
        control_config,
    )
    monkeypatch.setattr(
        "lazycloud.client_codegen.GatewayControlClient.from_endpoint",
        manifest_gateway,
    )

    output = tmp_path / "lazycloud_clients"
    result = runner.invoke(
        cli,
        [
            "--json",
            "client",
            "get",
            "billing",
            "--workspace",
            "platform",
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = _json_object(result.output, "client get output")
    assert _json_string(payload, "version")
    assert payload["app"] == "billing"
    assert payload["workspace"] == "platform"
    assert payload["package"] == "lazycloud_clients.billing"
    assert _json_path(payload, "resources", 0, "name") == "health"
    sys.path.insert(0, str(tmp_path))
    try:
        importlib.invalidate_caches()
        for module_name in list(sys.modules):
            if module_name == "lazycloud_clients" or module_name.startswith("lazycloud_clients."):
                sys.modules.pop(module_name)
        generated_module = importlib.import_module("lazycloud_clients.billing")
    finally:
        sys.path.remove(str(tmp_path))

    assert isinstance(generated_module, _GeneratedClientPackage)
    generated = generated_module

    def request_health(
        _handle: EndpointHandle,
        user_id: int,
        include_details: bool = False,
    ) -> EndpointResponse:
        assert user_id == 42
        assert include_details is True
        return EndpointResponse(
            status_code=200,
            headers={"content-type": ["application/json"]},
            content=b'{"status":"ok","details":{"code":200}}',
            url="https://example.com/health",
        )

    monkeypatch.setattr(EndpointHandle, "request", request_health)

    health = generated.health.request(user_id=42, include_details=True)

    assert (health.status, health.details.code) == ("ok", 200)
