from __future__ import annotations

from pydantic import Field, field_validator

from shared.http.base import HttpModel


class StartEndpointServeRequest(HttpModel):
    stub_id: str
    timeout: int = Field(default=0, ge=0)

    @field_validator("stub_id")
    @classmethod
    def required_stub_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            msg = "stub_id is required"
            raise ValueError(msg)
        return normalized


class StartEndpointServeResponse(HttpModel):
    container_id: str = ""


class EndpointForwardRequest(HttpModel):
    stub_id: str
    method: str = "GET"
    path: str = "/"
    query_params: dict[str, list[str]] = Field(default_factory=dict)
    headers: dict[str, list[str]] = Field(default_factory=dict)
    body: bytes = b""

    @field_validator("stub_id", "method")
    @classmethod
    def required_forward_string(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            msg = "endpoint forward fields cannot be empty"
            raise ValueError(msg)
        return normalized

    @field_validator("method")
    @classmethod
    def normalize_method(cls, value: str) -> str:
        return value.upper()

    @field_validator("path")
    @classmethod
    def normalize_path(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            return "/"
        return normalized if normalized.startswith("/") else f"/{normalized}"


class EndpointForwardResponse(HttpModel):
    status_code: int = Field(default=200, ge=100, le=599)
    headers: dict[str, list[str]] = Field(default_factory=dict)
    body: bytes = b""


__all__ = [
    "EndpointForwardRequest",
    "EndpointForwardResponse",
    "StartEndpointServeRequest",
    "StartEndpointServeResponse",
]
