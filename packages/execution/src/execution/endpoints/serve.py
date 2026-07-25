from __future__ import annotations

from pydantic import Field, computed_field, field_validator
from shared.contracts import ContractModel
from shared.deployment_records import Deployment
from shared.workload_keys import endpoint_keep_warm_lock_key

from execution.config import (
    ManagedPythonExecutable,
    managed_python_executable,
    parse_execution_python_version,
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


class EndpointServeCompletionPlan(ContractModel):
    workspace_name: str
    stub_id: str
    container_id: str
    release_keep_warm_lock_key: str
    keep_serve_lock: bool = True


def serve_deployment(
    deployment: Deployment,
    *,
    workspace_name: str = "default",
) -> EndpointServePlan:
    timeout = deployment.spec.resources.timeout_seconds
    timeout_seconds = timeout if timeout is not None else DEFAULT_ENDPOINT_SERVE_TIMEOUT_SECONDS
    python_version = parse_execution_python_version(deployment.spec.image.python_version)
    return plan_endpoint_serve(
        EndpointServeRequest(
            stub_id=deployment.id,
            workspace_name=workspace_name,
            timeout_seconds=timeout_seconds,
            python_executable=managed_python_executable(python_version),
        )
    )


def plan_endpoint_serve(request: EndpointServeRequest) -> EndpointServePlan:
    return EndpointServePlan(
        request=request,
        entrypoint=[request.python_executable, "-m", request.runner_module],
        instance_lock_key=endpoint_instance_lock_key(request.workspace_name, request.stub_id),
        serve_lock_key=endpoint_serve_lock_key(request.workspace_name, request.stub_id),
        serve_lock_ttl_seconds=request.timeout_seconds,
        wait_timeout_seconds=request.timeout_seconds,
    )


def plan_endpoint_serve_completion(
    request: EndpointServeRequest,
    *,
    container_id: str,
) -> EndpointServeCompletionPlan:
    return EndpointServeCompletionPlan(
        workspace_name=request.workspace_name,
        stub_id=request.stub_id,
        container_id=container_id,
        release_keep_warm_lock_key=endpoint_keep_warm_lock_key(
            request.workspace_name,
            request.stub_id,
            container_id,
        ),
    )
