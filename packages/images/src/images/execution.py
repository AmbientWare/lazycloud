from __future__ import annotations

from pydantic import Field
from shared.contracts import ContractModel
from shared.image_building.planning import ImageBuildPlan
from shared.image_building.records import BuildStatus

from images.building import (
    ImageBuildCredentialPlan,
    ImageBuildSessionPlan,
    ImageBuildStreamEventPlan,
)


class ImageBuildExecutionRequest(ContractModel):
    build_id: str
    workspace_id: str = ""
    image_id: str
    tag: str = ""
    build_dir: str
    dockerfile_path: str
    manifest_path: str
    plan: ImageBuildPlan
    session: ImageBuildSessionPlan
    credential_plan: ImageBuildCredentialPlan | None = None
    registry_credential_payload: str = Field(default="", repr=False)
    build_args: dict[str, str] = Field(default_factory=dict, repr=False)


class ImageBuildExecutionResult(ContractModel):
    status: BuildStatus
    events: list[ImageBuildStreamEventPlan] = Field(default_factory=list)
    command: list[str] = Field(default_factory=list)
    exit_code: int | None = None
    published_ref: str = ""
    artifact_path: str = ""
    cache_metadata: dict[str, str] = Field(default_factory=dict)
    reason: str = ""

    @property
    def complete(self) -> bool:
        return self.status is BuildStatus.Complete
