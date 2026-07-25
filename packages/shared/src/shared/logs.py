from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from shared.contracts import ContractModel
from shared.timestamps import utc_now


class LogEntry(ContractModel):
    id: str
    task_id: str
    stream: Literal["stdout", "stderr", "system"] = "system"
    message: str
    created_at: datetime = Field(default_factory=utc_now)


__all__ = ["LogEntry"]
