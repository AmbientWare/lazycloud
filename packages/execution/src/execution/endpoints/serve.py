from __future__ import annotations

from pydantic import Field, computed_field, field_validator
from shared.contracts import ContractModel

from execution.config import (
    ManagedPythonExecutable,
)
from execution.endpoints.keys import (
    DEFAULT_ENDPOINT_SERVE_TIMEOUT_SECONDS,
    endpoint_instance_lock_key,
    endpoint_serve_lock_key,
)

ENDPOINT_SERVE_RUNNER_MODULE = "runner.serve"
DEFAULT_ENDPOINT_PYTHON_EXECUTABLE = "python3.12"


class EndpointServeRequest(ContractModel):
    stub_id: str
    workspace_name: str = "default"
    workspace_id: str
    timeout_seconds: int = Field(default=DEFAULT_ENDPOINT_SERVE_TIMEOUT_SECONDS, gt=0)
    python_executable: ManagedPythonExecutable = DEFAULT_ENDPOINT_PYTHON_EXECUTABLE
    runner_module: str = ENDPOINT_SERVE_RUNNER_MODULE

    @field_validator("stub_id", "workspace_name", "python_executable", "runner_module")
    @classmethod
    def required_string(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            msg = "endpoint serve fields cannot be empty"
            raise ValueError(msg)
        return normalized


class EndpointServePlan(ContractModel):
    request: EndpointServeRequest
    entrypoint: list[str]
    instance_lock_key: str
    serve_lock_key: str
    serve_lock_ttl_seconds: int
    wait_timeout_seconds: int

    @computed_field
    @property
    def authorized(self) -> bool:
        return True


def plan_endpoint_serve(request: EndpointServeRequest) -> EndpointServePlan:
    return EndpointServePlan(
        request=request,
        entrypoint=[request.python_executable, "-m", request.runner_module],
        instance_lock_key=endpoint_instance_lock_key(request.workspace_id, request.stub_id),
        serve_lock_key=endpoint_serve_lock_key(request.workspace_id, request.stub_id),
        serve_lock_ttl_seconds=request.timeout_seconds,
        wait_timeout_seconds=request.timeout_seconds,
    )
