from __future__ import annotations

from lazycloud._shared.deployments import DeploymentKind
from lazycloud._shared.enums import StringEnum
from lazycloud._shared.tasks import TaskStatus
from lazycloud.contracts import ContractModel


class LifecycleHookName(StringEnum):
    Start = "on_start"
    Running = "on_running"
    Success = "on_success"
    Error = "on_error"
    Retry = "on_retry"
    Failure = "on_failure"
    Finish = "on_finish"


class LifecycleStartupContext(ContractModel):
    hook: LifecycleHookName = LifecycleHookName.Start
    stub_id: str = ""
    workspace_id: str = ""
    workspace_name: str = ""
    app_id: str = ""
    container_id: str = ""
    container_hostname: str = ""
    handler: str = ""
    resource_kind: DeploymentKind = DeploymentKind.Function


class LifecycleTaskContext(ContractModel):
    hook: LifecycleHookName
    task_id: str
    status: TaskStatus
    stub_id: str = ""
    root_task_id: str = ""
    parent_task_id: str = ""
    workspace_id: str = ""
    workspace_name: str = ""
    app_id: str = ""
    container_id: str = ""
    container_hostname: str = ""
    handler: str = ""
    resource_kind: DeploymentKind = DeploymentKind.Function
    attempt_number: int = 0
    max_attempts: int = 1
    duration_seconds: float = 0.0
    error_type: str = ""
    error_message: str = ""
    retry_scheduled: bool = False
    result_available: bool = False


__all__ = [
    "LifecycleHookName",
    "LifecycleStartupContext",
    "LifecycleTaskContext",
]
