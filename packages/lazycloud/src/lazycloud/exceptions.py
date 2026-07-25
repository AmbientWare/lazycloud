from __future__ import annotations


class SdkError(RuntimeError):
    pass


class ConfigurationError(SdkError):
    pass


class RunnerError(SdkError):
    pass


class InvalidFunctionArgumentsError(RunnerError):
    pass


class TaskNotFoundError(SdkError):
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        super().__init__(f"task not found: {task_id}")


class DeploymentNotFoundError(SdkError):
    def __init__(self, deployment_id: str) -> None:
        self.deployment_id = deployment_id
        super().__init__(f"deployment not found: {deployment_id}")


class WorkspaceNotFoundError(SdkError):
    def __init__(self, workspace: str) -> None:
        self.workspace = workspace
        super().__init__(f"workspace not found: {workspace}")


class ClientGenerationError(SdkError):
    pass


class ObjectUploadError(SdkError):
    pass


class VolumeUploadError(ObjectUploadError):
    pass


class RetryableError(SdkError):
    def __init__(self, tries: int, message: str) -> None:
        self.tries = tries
        self.message = message
        super().__init__(f"retryable error after {tries} attempts: {message}")


class SandboxConnectionError(SdkError):
    pass


class SandboxProcessError(SdkError):
    pass


class SandboxFileSystemError(SdkError):
    def __init__(
        self,
        message: str,
        *,
        operation: str = "",
        path: str = "",
        container_id: str = "",
    ) -> None:
        self.operation = operation
        self.path = path
        self.container_id = container_id
        super().__init__(message)


__all__ = [
    "ClientGenerationError",
    "ConfigurationError",
    "DeploymentNotFoundError",
    "InvalidFunctionArgumentsError",
    "ObjectUploadError",
    "RetryableError",
    "RunnerError",
    "SandboxConnectionError",
    "SandboxFileSystemError",
    "SandboxProcessError",
    "SdkError",
    "TaskNotFoundError",
    "VolumeUploadError",
    "WorkspaceNotFoundError",
]
