from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from pydantic import Field
from shared.container_requests import WorkerStartupKind
from shared.contracts import ContractModel
from shared.image_building.authoring import LinuxArchitecture

from worker.execution import OciMount, OciMountType
from worker.managed_runtime_catalog import ManagedRuntimeCatalog

# Where the runtime is baked into the worker image. Distinct from
# MANAGED_RUNTIME_CONTAINER_ROOT below, which is where it is mounted inside a
# user's container — collapsing the two leaves the worker reading a path that
# only exists on the other side of the bind.
MANAGED_RUNTIME_IMAGE_ROOT = Path("/opt/lazycloud/managed-runtime-source")
MANAGED_RUNTIME_CONTAINER_ROOT = "/opt/lazycloud/managed-runtime"
MANAGED_RUNTIME_LAUNCHER_PATH = f"{MANAGED_RUNTIME_CONTAINER_ROOT}/launcher.py"
MANAGED_RUNTIME_CATALOG_DIGEST_ENV = "LAZYCLOUD_MANAGED_RUNTIME_CATALOG_DIGEST"
MANAGED_RUNTIME_DIGEST_ENV = "LAZYCLOUD_MANAGED_RUNTIME_DIGEST"
MANAGED_RUNTIME_MODULES = frozenset(
    {
        "runner.function",
        "runner.serve",
    }
)
MANAGED_RUNTIME_STARTUP_KINDS = frozenset(
    {
        WorkerStartupKind.Function,
        WorkerStartupKind.Endpoint,
        WorkerStartupKind.Asgi,
    }
)


class ManagedRuntimePlan(ContractModel):
    enabled: bool = False
    digest: str = ""
    artifact_digest: str = ""
    command: list[str] = Field(default_factory=list)
    python_paths: list[str] = Field(default_factory=list)
    mounts: list[OciMount] = Field(default_factory=list)

    def python_path(self, existing: str = "") -> str:
        paths = [*self.python_paths, *(item for item in existing.split(":") if item)]
        return ":".join(dict.fromkeys(paths))


def plan_managed_runtime(
    startup_kind: WorkerStartupKind,
    *,
    catalog: ManagedRuntimeCatalog | None,
    architecture: LinuxArchitecture,
    command: Sequence[str],
    user_code_path: str = "/workspace",
) -> ManagedRuntimePlan:
    if startup_kind not in MANAGED_RUNTIME_STARTUP_KINDS:
        return ManagedRuntimePlan(command=list(command))
    if catalog is None:
        raise RuntimeError("managed runtime artifact catalog is unavailable")
    target_python = managed_runtime_python_version(command)
    if catalog.selected_python != target_python:
        raise RuntimeError(
            "managed runtime catalog selection mismatch: "
            f"entrypoint uses Python {target_python}, catalog selected {catalog.selected_python}"
        )
    if catalog.selected_architecture is not architecture:
        raise RuntimeError(
            "managed runtime catalog architecture mismatch: "
            f"workload uses {architecture.value}, "
            f"catalog selected {catalog.selected_architecture.value}"
        )
    wrapped_command = _managed_runtime_command(command)
    return ManagedRuntimePlan(
        enabled=True,
        digest=catalog.digest,
        artifact_digest=catalog.selected_artifact.digest,
        command=wrapped_command,
        python_paths=[user_code_path] if user_code_path else [],
        mounts=[
            OciMount(
                mount_type=OciMountType.Bind,
                source=catalog.host_path,
                destination=MANAGED_RUNTIME_CONTAINER_ROOT,
                options=["ro", "rbind", "rprivate", "nosuid", "nodev"],
            )
        ],
    )


def managed_runtime_python_version(command: Sequence[str]) -> str:
    if not command:
        raise RuntimeError("managed workload entrypoint has no target Python interpreter")
    executable = Path(command[0]).name
    match = re.fullmatch(r"(?:python|micromamba)(3\.\d+)", executable)
    if match is None:
        raise RuntimeError(
            "managed workload entrypoint must use an explicit target interpreter such as "
            "python3.10, python3.11, python3.12, or the matching micromamba executable"
        )
    return match.group(1)


def _managed_runtime_command(command: Sequence[str]) -> list[str]:
    values = list(command)
    if len(values) < 3 or values[1] != "-m" or values[2] not in MANAGED_RUNTIME_MODULES:
        raise RuntimeError(
            "managed workload entrypoint must be a target Python interpreter followed by "
            "-m runner.function or runner.serve"
        )
    return [values[0], MANAGED_RUNTIME_LAUNCHER_PATH, values[2], *values[3:]]


__all__ = [
    "MANAGED_RUNTIME_CATALOG_DIGEST_ENV",
    "MANAGED_RUNTIME_CONTAINER_ROOT",
    "MANAGED_RUNTIME_DIGEST_ENV",
    "MANAGED_RUNTIME_IMAGE_ROOT",
    "MANAGED_RUNTIME_LAUNCHER_PATH",
    "MANAGED_RUNTIME_MODULES",
    "MANAGED_RUNTIME_STARTUP_KINDS",
    "ManagedRuntimePlan",
    "managed_runtime_python_version",
    "plan_managed_runtime",
]
