from __future__ import annotations

from pydantic import Field

from shared.contracts import ContractModel
from shared.image_building.authoring import ImageSpec


class ImageBuildPlan(ContractModel):
    spec: ImageSpec
    image_id: str
    cache_key: str
    dockerfile: str
    context_digest: str | None = None
    credential_keys: list[str] = Field(default_factory=list)
