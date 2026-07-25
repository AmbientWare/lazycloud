from __future__ import annotations

from typing import Protocol

from pydantic import Field

from shared.http.base import HttpModel


class _ShellProxyPlan(Protocol):
    route_path: str
    container_id: str
    stub_id: str
    worker_port: int
    buffer_size_bytes: int
    keepalive_interval_seconds: int
    dial_timeout_seconds: int


class CreateStandaloneShellRequest(HttpModel):
    stub_id: str


class StandaloneShellSession(HttpModel):
    container_id: str
    username: str
    password: str


class CreateStandaloneShellResponse(StandaloneShellSession):
    websocket_ticket: str = Field(min_length=1)


class CreateShellInExistingContainerRequest(HttpModel):
    container_id: str


class ExistingContainerShellSession(HttpModel):
    username: str
    password: str
    stub_id: str


class CreateShellInExistingContainerResponse(ExistingContainerShellSession):
    websocket_ticket: str = Field(min_length=1)


class ShellConnectPlanResponse(HttpModel):
    route_path: str
    container_id: str
    stub_id: str
    worker_port: int
    buffer_size_bytes: int
    keepalive_interval_seconds: int
    dial_timeout_seconds: int

    @classmethod
    def from_plan(cls, plan: _ShellProxyPlan) -> ShellConnectPlanResponse:
        return cls(
            route_path=plan.route_path,
            container_id=plan.container_id,
            stub_id=plan.stub_id,
            worker_port=plan.worker_port,
            buffer_size_bytes=plan.buffer_size_bytes,
            keepalive_interval_seconds=plan.keepalive_interval_seconds,
            dial_timeout_seconds=plan.dial_timeout_seconds,
        )


__all__ = [
    "CreateShellInExistingContainerRequest",
    "CreateShellInExistingContainerResponse",
    "CreateStandaloneShellRequest",
    "CreateStandaloneShellResponse",
    "ExistingContainerShellSession",
    "ShellConnectPlanResponse",
    "StandaloneShellSession",
]
