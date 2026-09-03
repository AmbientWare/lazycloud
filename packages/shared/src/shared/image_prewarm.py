from __future__ import annotations

from pydantic import Field

from shared.contracts import ContractModel


class WorkerImagePrewarmTarget(ContractModel):
    workspace_id: str = Field(min_length=1)
    stub_id: str = ""
    image_id: str = Field(min_length=1)
    archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class WorkerImagePrewarmArchive(ContractModel):
    image_id: str = Field(min_length=1)
    archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    archive_size_bytes: int = Field(default=0, ge=0)
    image_archive_url: str = Field(default="", repr=False)
