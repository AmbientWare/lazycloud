from __future__ import annotations

from pydantic import Field, JsonValue, field_validator

from shared.contracts import ContractModel
from shared.deployments import DeploymentKind
from shared.enums import StringEnum
from shared.tasks import TaskStatus


class LifecycleHookName(StringEnum):
    Start = "on_start"
    Running = "on_running"
    Success = "on_success"
    Error = "on_error"
    Retry = "on_retry"
    Failure = "on_failure"
    Finish = "on_finish"


class LifecycleHooks(ContractModel):
    on_start: tuple[str, ...] = Field(default_factory=tuple)
    on_running: tuple[str, ...] = Field(default_factory=tuple)
    on_success: tuple[str, ...] = Field(default_factory=tuple)
    on_error: tuple[str, ...] = Field(default_factory=tuple)
    on_retry: tuple[str, ...] = Field(default_factory=tuple)
    on_failure: tuple[str, ...] = Field(default_factory=tuple)
    on_finish: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("*", mode="before")
    @classmethod
    def normalize_references(cls, value: JsonValue) -> tuple[str, ...]:
        if value is None or value == "":
            return ()
        if isinstance(value, str):
            return (value,)
        if isinstance(value, list | tuple):
            return tuple(str(item) for item in value if str(item))
        msg = "lifecycle hook references must be strings or string lists"
        raise TypeError(msg)

    def refs(self, hook: LifecycleHookName) -> tuple[str, ...]:
        return {
            LifecycleHookName.Start: self.on_start,
            LifecycleHookName.Running: self.on_running,
            LifecycleHookName.Success: self.on_success,
            LifecycleHookName.Error: self.on_error,
            LifecycleHookName.Retry: self.on_retry,
            LifecycleHookName.Failure: self.on_failure,
            LifecycleHookName.Finish: self.on_finish,
        }[hook]

    @property
    def configured(self) -> bool:
        return any(self.refs(hook) for hook in LifecycleHookName)


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
    "LifecycleHooks",
    "LifecycleStartupContext",
    "LifecycleTaskContext",
]
