from __future__ import annotations

from datetime import datetime

from pydantic import Field

from shared.contracts import ContractModel


class UsageQuery(ContractModel):
    workspace_id: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None
    labels: dict[str, str] = Field(default_factory=dict)


__all__ = ["UsageQuery"]
