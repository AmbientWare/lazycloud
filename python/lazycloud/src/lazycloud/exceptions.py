from __future__ import annotations

from typing import Any


class SdkError(RuntimeError):
    pass


class ConfigurationError(SdkError):
    pass


class RunnerError(SdkError):
    pass


class InvalidFunctionArgumentsError(RunnerError):
    pass


class UnsupportedFeatureError(SdkError):
    """A declared option the platform cannot run yet."""

    def __init__(self, target: str, features: list[str]) -> None:
        self.target = target
        self.features = features
        super().__init__(
            f"{target} uses options the platform does not support yet: " + ", ".join(features)
        )


class FunctionNotDeployedError(SdkError):
    def __init__(self, app: str, function: str, workspace: str) -> None:
        self.app = app
        self.function = function
        self.workspace = workspace
        super().__init__(
            f"function {app}.{function} is not deployed in workspace {workspace}; "
            "run `lazycloud deploy` first"
        )


class RemoteTaskError(SdkError):
    """A task finished without a result and its exception could not be restored."""

    def __init__(
        self,
        task_id: str,
        *,
        kind: str,
        type: str | None,
        message: str,
        traceback: str | None,
    ) -> None:
        self.task_id = task_id
        self.kind = kind
        self.type = type
        self.message = message
        self.traceback = traceback
        label = f"{type}: {message}" if type else message
        text = f"task {task_id} failed ({kind}): {label}"
        if traceback:
            text = f"{text}\n\nRemote traceback:\n{traceback.rstrip()}"
        super().__init__(text)


class MapSubmissionError(SdkError):
    """A later submit batch failed; the tasks already admitted keep running."""

    def __init__(self, submitted: list[Any], requested: int, cause: Exception) -> None:
        self.submitted = submitted
        self.requested = requested
        super().__init__(
            f"submitted {len(submitted)} of {requested} inputs before failing: {cause}"
        )


class TaskCancelledError(SdkError):
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        super().__init__(f"task {task_id} was cancelled")


class TaskNotFoundError(SdkError):
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        super().__init__(f"task not found: {task_id}")


class DeploymentNotFoundError(SdkError):
    def __init__(self, reference: str) -> None:
        self.reference = reference
        super().__init__(f"deployment not found: {reference}")


class AmbiguousDeploymentError(SdkError):
    """A deployment name that several apps use."""

    def __init__(self, name: str, apps: list[str]) -> None:
        self.name = name
        self.apps = apps
        super().__init__(
            f"deployment {name} exists in apps {', '.join(apps)}; use the deployment id"
        )


class WorkspaceNotFoundError(SdkError):
    def __init__(self, workspace: str) -> None:
        self.workspace = workspace
        super().__init__(f"workspace not found: {workspace}")


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
    "AmbiguousDeploymentError",
    "ConfigurationError",
    "DeploymentNotFoundError",
    "FunctionNotDeployedError",
    "InvalidFunctionArgumentsError",
    "MapSubmissionError",
    "ObjectUploadError",
    "RemoteTaskError",
    "RetryableError",
    "RunnerError",
    "SandboxConnectionError",
    "SandboxFileSystemError",
    "SandboxProcessError",
    "SdkError",
    "TaskCancelledError",
    "TaskNotFoundError",
    "UnsupportedFeatureError",
    "VolumeUploadError",
    "WorkspaceNotFoundError",
]
