from __future__ import annotations

from enum import StrEnum

from database.records.apps import AppRecord, StubKind, StubRecord
from pydantic import JsonValue
from shared.contracts import ContractModel
from shared.deployment_records import Deployment
from shared.identity import ConcurrencyLimitRecord, WorkspaceRecord


class ConcurrencyAcquireStatus(StrEnum):
    Acquired = "acquired"
    Saturated = "saturated"
    Released = "released"


class PublicStubConfigKey(StrEnum):
    Inputs = "inputs"
    Outputs = "outputs"
    TaskPolicy = "task_policy"
    PythonVersion = "python_version"
    Runtime = "runtime"


class ConcurrencyAcquireResult(ContractModel):
    status: ConcurrencyAcquireStatus
    acquired: bool
    record: ConcurrencyLimitRecord
    available_before: int
    available_after: int
    reason: str


class StubUrlPlan(ContractModel):
    stub: StubRecord
    url: str
    external_url: str
    route_kind: StubKind
    deployment: Deployment | None = None


class StubConfigUpdateResult(ContractModel):
    stub: StubRecord
    updated_fields: tuple[str, ...]
    message: str


class StubCloneOverride(ContractModel):
    cpu: int | None = None
    memory: int | None = None
    gpu: str | None = None
    gpu_count: int | None = None


class StubCloneResult(ContractModel):
    source_stub: StubRecord
    cloned_stub: StubRecord
    app: AppRecord
    copied_config: dict[str, JsonValue]
    copied_objects: tuple[str, ...] = ()


class WorkspaceCreateResult(ContractModel):
    workspace_id: str
    token: str
    workspace: WorkspaceRecord


class WorkspaceConfigExport(ContractModel):
    gateway_http_host: str
    gateway_http_port: int
    gateway_http_tls: bool
    gateway_grpc_host: str
    gateway_grpc_port: int
    gateway_grpc_tls: bool
    workspace_id: str
    token: str = ""
