from __future__ import annotations

import shutil
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import Field
from shared.contracts import ContractModel
from shared.image_building.records import BuildStatus, ImageBuildRecord


class ImageBuildCleanupAction(StrEnum):
    KeepArtifacts = "keep-artifacts"
    RemoveBuildDirectory = "remove-build-directory"
    StopBuildContainer = "stop-build-container"


class ImageBuildCleanupStatus(StrEnum):
    Skipped = "skipped"
    Planned = "planned"
    Complete = "complete"
    Error = "error"


class ImageBuildCleanupPlan(ContractModel):
    status: ImageBuildCleanupStatus
    build_id: str = ""
    actions: list[ImageBuildCleanupAction] = Field(default_factory=list)
    build_dir: str = ""
    container_id: str = ""
    reason: str = ""

    @property
    def should_cleanup(self) -> bool:
        return any(action is not ImageBuildCleanupAction.KeepArtifacts for action in self.actions)


class ImageBuildCleanupResult(ContractModel):
    status: ImageBuildCleanupStatus
    actions: list[ImageBuildCleanupAction] = Field(default_factory=list)
    removed_paths: list[str] = Field(default_factory=list)
    reason: str = ""

    @property
    def complete(self) -> bool:
        return self.status is ImageBuildCleanupStatus.Complete


class ImageBuildCleanupExecutor(Protocol):
    def cleanup(self, plan: ImageBuildCleanupPlan) -> ImageBuildCleanupResult: ...


@dataclass(slots=True)
class LocalImageBuildCleanupExecutor:
    def cleanup(self, plan: ImageBuildCleanupPlan) -> ImageBuildCleanupResult:
        if not plan.should_cleanup:
            return ImageBuildCleanupResult(
                status=ImageBuildCleanupStatus.Skipped,
                actions=list(plan.actions),
                reason=plan.reason,
            )
        removed_paths: list[str] = []
        if ImageBuildCleanupAction.RemoveBuildDirectory in plan.actions and plan.build_dir:
            path = Path(plan.build_dir)
            if path.exists():
                try:
                    shutil.rmtree(path)
                except OSError as exc:
                    return ImageBuildCleanupResult(
                        status=ImageBuildCleanupStatus.Error,
                        actions=list(plan.actions),
                        removed_paths=removed_paths,
                        reason=str(exc),
                    )
                removed_paths.append(str(path))
        return ImageBuildCleanupResult(
            status=ImageBuildCleanupStatus.Complete,
            actions=list(plan.actions),
            removed_paths=removed_paths,
            reason="image build cleanup complete",
        )


def plan_image_build_cleanup(
    build: ImageBuildRecord,
    *,
    keep_artifacts: bool = True,
    container_id: str = "",
) -> ImageBuildCleanupPlan:
    if build.status not in {
        BuildStatus.Complete,
        BuildStatus.Failed,
        BuildStatus.Cancelled,
        BuildStatus.Timeout,
    }:
        return ImageBuildCleanupPlan(
            status=ImageBuildCleanupStatus.Skipped,
            build_id=build.id,
            reason="image build is not terminal",
        )

    actions: list[ImageBuildCleanupAction] = []
    if keep_artifacts:
        actions.append(ImageBuildCleanupAction.KeepArtifacts)
    else:
        actions.append(ImageBuildCleanupAction.RemoveBuildDirectory)
    if container_id:
        actions.append(ImageBuildCleanupAction.StopBuildContainer)
    return ImageBuildCleanupPlan(
        status=ImageBuildCleanupStatus.Planned,
        build_id=build.id,
        actions=actions,
        build_dir=_build_dir(build),
        container_id=container_id,
        reason="image build cleanup planned",
    )


def _build_dir(build: ImageBuildRecord) -> str:
    if build.manifest_path:
        return str(Path(build.manifest_path).parent)
    if build.artifact_path and Path(build.artifact_path).is_dir():
        return build.artifact_path
    return ""
