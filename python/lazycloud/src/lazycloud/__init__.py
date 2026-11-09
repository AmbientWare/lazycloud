from __future__ import annotations

from functools import cache
from importlib import import_module
from typing import TYPE_CHECKING

# Importing the namesake submodule would overwrite a deferred callable export.
from lazycloud.progress import progress

if TYPE_CHECKING:
    from lazycloud import env, schema
    from lazycloud._shared.autoscaling import Autoscaler
    from lazycloud._shared.gpu import GpuType
    from lazycloud._shared.image_building.authoring import LinuxArchitecture, PythonVersion
    from lazycloud._shared.task_context import current_root_task_id, current_task_id
    from lazycloud._shared.tasks import RetryBackoff, RetryPolicy, TaskPolicy
    from lazycloud.abstractions.app import App
    from lazycloud.abstractions.artifact import Artifact
    from lazycloud.abstractions.disk import Disk
    from lazycloud.abstractions.image import Image
    from lazycloud.abstractions.map import Map
    from lazycloud.abstractions.pod import Container
    from lazycloud.abstractions.queue import Queue
    from lazycloud.abstractions.sandbox import (
        Sandbox,
        SandboxFileInfo,
        SandboxFilePosition,
        SandboxFileSearchMatch,
        SandboxFileSearchRange,
        SandboxFileSearchResult,
        SandboxFileSystem,
        SandboxInstance,
        SandboxProcess,
        SandboxProcessManager,
        SandboxProcessResponse,
        SandboxProcessStream,
    )
    from lazycloud.abstractions.secret import Secret
    from lazycloud.abstractions.volume import CloudBucket, CloudBucketConfig, Volume
    from lazycloud.agent_harness import AgentHarness
    from lazycloud.contracts.api import TaskPendingProgress, TaskPendingReason
    from lazycloud.exceptions import (
        SandboxConnectionError,
        SandboxFileSystemError,
        SandboxProcessError,
        SandboxProcessTimeoutError,
    )
    from lazycloud.progress import PendingProgressCallback
    from lazycloud.session.deployment import Deployment
    from lazycloud.session.task import FunctionCall, Task
    from lazycloud.terminal import output


# Each export loads its module on first use, so `import lazycloud` stays cheap.
_EXPORTS = {
    "AgentHarness": "lazycloud.agent_harness",
    "App": "lazycloud.abstractions.app",
    "Artifact": "lazycloud.abstractions.artifact",
    "Autoscaler": "lazycloud._shared.autoscaling",
    "CloudBucket": "lazycloud.abstractions.volume",
    "CloudBucketConfig": "lazycloud.abstractions.volume",
    "Container": "lazycloud.abstractions.pod",
    "Deployment": "lazycloud.session.deployment",
    "Disk": "lazycloud.abstractions.disk",
    "FunctionCall": "lazycloud.session.task",
    "GpuType": "lazycloud._shared.gpu",
    "Image": "lazycloud.abstractions.image",
    "LinuxArchitecture": "lazycloud._shared.image_building.authoring",
    "Map": "lazycloud.abstractions.map",
    "PendingProgressCallback": "lazycloud.progress",
    "PythonVersion": "lazycloud._shared.image_building.authoring",
    "Queue": "lazycloud.abstractions.queue",
    "RetryBackoff": "lazycloud._shared.tasks",
    "RetryPolicy": "lazycloud._shared.tasks",
    "Sandbox": "lazycloud.abstractions.sandbox",
    "SandboxConnectionError": "lazycloud.exceptions",
    "SandboxFileInfo": "lazycloud.abstractions.sandbox",
    "SandboxFilePosition": "lazycloud.abstractions.sandbox",
    "SandboxFileSearchMatch": "lazycloud.abstractions.sandbox",
    "SandboxFileSearchRange": "lazycloud.abstractions.sandbox",
    "SandboxFileSearchResult": "lazycloud.abstractions.sandbox",
    "SandboxFileSystem": "lazycloud.abstractions.sandbox",
    "SandboxFileSystemError": "lazycloud.exceptions",
    "SandboxInstance": "lazycloud.abstractions.sandbox",
    "SandboxProcess": "lazycloud.abstractions.sandbox",
    "SandboxProcessError": "lazycloud.exceptions",
    "SandboxProcessManager": "lazycloud.abstractions.sandbox",
    "SandboxProcessResponse": "lazycloud.abstractions.sandbox",
    "SandboxProcessStream": "lazycloud.abstractions.sandbox",
    "SandboxProcessTimeoutError": "lazycloud.exceptions",
    "Secret": "lazycloud.abstractions.secret",
    "Task": "lazycloud.session.task",
    "TaskPendingProgress": "lazycloud.contracts.api",
    "TaskPendingReason": "lazycloud.contracts.api",
    "TaskPolicy": "lazycloud._shared.tasks",
    "Volume": "lazycloud.abstractions.volume",
    "current_root_task_id": "lazycloud._shared.task_context",
    "current_task_id": "lazycloud._shared.task_context",
    "output": "lazycloud.terminal",
}


@cache
def __getattr__(name: str):
    if name in {"env", "schema"}:
        return import_module(f"lazycloud.{name}")
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(module), name)


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
