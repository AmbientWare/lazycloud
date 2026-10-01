from __future__ import annotations

from functools import cache
from importlib import import_module
from typing import TYPE_CHECKING

# Importing the namesake submodule would overwrite a deferred callable export.
from lazycloud.progress import progress

if TYPE_CHECKING:
    from shared.api import Deployment
    from shared.autoscaling import Autoscaler
    from shared.gpu import GpuType
    from shared.image_building.authoring import LinuxArchitecture, PythonVersion
    from shared.task_context import current_root_task_id, current_task_id
    from shared.tasks import RetryBackoff, RetryPolicy, TaskPolicy

    from lazycloud import env, schema
    from lazycloud.abstractions.app import App
    from lazycloud.abstractions.artifact import Artifact
    from lazycloud.abstractions.disk import Disk
    from lazycloud.abstractions.image import Image
    from lazycloud.abstractions.map import Map
    from lazycloud.abstractions.pod import Container
    from lazycloud.abstractions.queue import Queue
    from lazycloud.abstractions.sandbox import (
        Sandbox,
        SandboxConnectionError,
        SandboxFileInfo,
        SandboxFilePosition,
        SandboxFileSearchMatch,
        SandboxFileSearchRange,
        SandboxFileSearchResult,
        SandboxFileSystem,
        SandboxFileSystemError,
        SandboxInstance,
        SandboxProcess,
        SandboxProcessError,
        SandboxProcessManager,
        SandboxProcessResponse,
        SandboxProcessStream,
        SandboxProcessTimeoutError,
    )
    from lazycloud.abstractions.secret import Secret
    from lazycloud.abstractions.volume import CloudBucket, CloudBucketConfig, Volume
    from lazycloud.agent_harness import AgentHarness
    from lazycloud.progress import (
        PendingProgressCallback,
        TaskPendingProgress,
        TaskPendingReason,
    )
    from lazycloud.session.task import FunctionCall, Task
    from lazycloud.terminal import output


@cache
def __getattr__(name: str):
    match name:
        case "FunctionCall":
            from lazycloud.session.task import FunctionCall

            return FunctionCall
        case "Autoscaler":
            from shared.autoscaling import Autoscaler

            return Autoscaler
        case "GpuType":
            from shared.gpu import GpuType

            return GpuType
        case "LinuxArchitecture" | "PythonVersion":
            from shared.image_building.authoring import LinuxArchitecture, PythonVersion

            if name == "LinuxArchitecture":
                return LinuxArchitecture
            if name == "PythonVersion":
                return PythonVersion
        case "current_root_task_id" | "current_task_id":
            from shared.task_context import current_root_task_id, current_task_id

            if name == "current_root_task_id":
                return current_root_task_id
            if name == "current_task_id":
                return current_task_id
        case "RetryBackoff" | "RetryPolicy" | "TaskPolicy":
            from shared.tasks import RetryBackoff, RetryPolicy, TaskPolicy

            if name == "RetryBackoff":
                return RetryBackoff
            if name == "RetryPolicy":
                return RetryPolicy
            if name == "TaskPolicy":
                return TaskPolicy
        case "env" | "schema":
            return import_module(f"lazycloud.{name}")
        case "App":
            from lazycloud.abstractions.app import App

            return App
        case "Artifact":
            from lazycloud.abstractions.artifact import Artifact

            return Artifact
        case "Disk":
            from lazycloud.abstractions.disk import Disk

            return Disk
        case "Image":
            from lazycloud.abstractions.image import Image

            return Image
        case "Map":
            from lazycloud.abstractions.map import Map

            return Map
        case "Container":
            from lazycloud.abstractions.pod import Container

            return Container
        case "Queue":
            from lazycloud.abstractions.queue import Queue

            return Queue
        case (
            "Sandbox"
            | "SandboxConnectionError"
            | "SandboxFileInfo"
            | "SandboxFilePosition"
            | "SandboxFileSearchMatch"
            | "SandboxFileSearchRange"
            | "SandboxFileSearchResult"
            | "SandboxFileSystem"
            | "SandboxFileSystemError"
            | "SandboxInstance"
            | "SandboxProcess"
            | "SandboxProcessError"
            | "SandboxProcessManager"
            | "SandboxProcessResponse"
            | "SandboxProcessStream"
            | "SandboxProcessTimeoutError"
        ):
            from lazycloud.abstractions.sandbox import (
                Sandbox,
                SandboxConnectionError,
                SandboxFileInfo,
                SandboxFilePosition,
                SandboxFileSearchMatch,
                SandboxFileSearchRange,
                SandboxFileSearchResult,
                SandboxFileSystem,
                SandboxFileSystemError,
                SandboxInstance,
                SandboxProcess,
                SandboxProcessError,
                SandboxProcessManager,
                SandboxProcessResponse,
                SandboxProcessStream,
                SandboxProcessTimeoutError,
            )

            if name == "Sandbox":
                return Sandbox
            if name == "SandboxConnectionError":
                return SandboxConnectionError
            if name == "SandboxFileInfo":
                return SandboxFileInfo
            if name == "SandboxFilePosition":
                return SandboxFilePosition
            if name == "SandboxFileSearchMatch":
                return SandboxFileSearchMatch
            if name == "SandboxFileSearchRange":
                return SandboxFileSearchRange
            if name == "SandboxFileSearchResult":
                return SandboxFileSearchResult
            if name == "SandboxFileSystem":
                return SandboxFileSystem
            if name == "SandboxFileSystemError":
                return SandboxFileSystemError
            if name == "SandboxInstance":
                return SandboxInstance
            if name == "SandboxProcess":
                return SandboxProcess
            if name == "SandboxProcessError":
                return SandboxProcessError
            if name == "SandboxProcessManager":
                return SandboxProcessManager
            if name == "SandboxProcessResponse":
                return SandboxProcessResponse
            if name == "SandboxProcessStream":
                return SandboxProcessStream
            if name == "SandboxProcessTimeoutError":
                return SandboxProcessTimeoutError
        case "Secret":
            from lazycloud.abstractions.secret import Secret

            return Secret
        case "CloudBucket" | "CloudBucketConfig" | "Volume":
            from lazycloud.abstractions.volume import CloudBucket, CloudBucketConfig, Volume

            if name == "CloudBucket":
                return CloudBucket
            if name == "CloudBucketConfig":
                return CloudBucketConfig
            if name == "Volume":
                return Volume
        case "AgentHarness":
            from lazycloud.agent_harness import AgentHarness

            return AgentHarness
        case "PendingProgressCallback" | "TaskPendingProgress" | "TaskPendingReason":
            from lazycloud.progress import (
                PendingProgressCallback,
                TaskPendingProgress,
                TaskPendingReason,
            )

            if name == "PendingProgressCallback":
                return PendingProgressCallback
            if name == "TaskPendingProgress":
                return TaskPendingProgress
            if name == "TaskPendingReason":
                return TaskPendingReason
        case "Deployment":
            from shared.api import Deployment

            return Deployment
        case "Task":
            from lazycloud.session.task import Task

            return Task
        case "output":
            from lazycloud.terminal import output

            return output
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


__all__ = [
    "AgentHarness",
    "App",
    "Artifact",
    "Autoscaler",
    "CloudBucket",
    "CloudBucketConfig",
    "Container",
    "Deployment",
    "Disk",
    "FunctionCall",
    "GpuType",
    "Image",
    "LinuxArchitecture",
    "Map",
    "PendingProgressCallback",
    "PythonVersion",
    "Queue",
    "RetryBackoff",
    "RetryPolicy",
    "Sandbox",
    "SandboxConnectionError",
    "SandboxFileInfo",
    "SandboxFilePosition",
    "SandboxFileSearchMatch",
    "SandboxFileSearchRange",
    "SandboxFileSearchResult",
    "SandboxFileSystem",
    "SandboxFileSystemError",
    "SandboxInstance",
    "SandboxProcess",
    "SandboxProcessError",
    "SandboxProcessManager",
    "SandboxProcessResponse",
    "SandboxProcessStream",
    "SandboxProcessTimeoutError",
    "Secret",
    "Task",
    "TaskPendingProgress",
    "TaskPendingReason",
    "TaskPolicy",
    "Volume",
    "current_root_task_id",
    "current_task_id",
    "env",
    "output",
    "progress",
    "schema",
]
