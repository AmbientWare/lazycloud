from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from shared.contracts import ContractModel
from shared.enums import StringEnum
from shared.timestamps import utc_now


class ContainerLogEntryKind(StringEnum):
    """What a captured entry is, which decides whether a customer sees it.

    Only ``Output`` is the container speaking. ``Flush`` is a capture barrier with no
    message; ``Dropped`` and ``Diagnostic`` are the worker telling the reader why
    output is missing, which is worth more to them than silence.
    """

    Output = "output"
    Dropped = "dropped"
    Flush = "flush"
    Diagnostic = "diagnostic"


class LogEntry(ContractModel):
    id: str
    task_id: str
    stream: Literal["stdout", "stderr", "system"] = "system"
    message: str
    created_at: datetime = Field(default_factory=utc_now)


__all__ = ["ContainerLogEntryKind", "LogEntry"]
