"""Records shared by the API, the checks and the two maps."""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StringConstraints

MAX_WATCHES = 100

Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
Focus = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
Selector = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class WatchRequest(BaseModel):
    """A page to watch, optionally narrowed to one element and one kind of change."""

    model_config = ConfigDict(extra="forbid")

    url: HttpUrl
    label: Label | None = None
    focus: Focus | None = None
    selector: Selector | None = None


class Watch(WatchRequest):
    id: str

    @classmethod
    def from_request(cls, request: WatchRequest) -> Watch:
        # The same page and element always get the same ID, so adding twice updates.
        key = f"{request.url}\n{request.selector or ''}"
        return cls(id=hashlib.sha256(key.encode()).hexdigest()[:16], **request.model_dump())


class Outcome(StrEnum):
    BASELINE = "baseline"
    UNCHANGED = "unchanged"
    MINOR_CHANGE = "minor_change"
    ALERTED = "alerted"


class Snapshot(BaseModel):
    """The page text one run saw, and what that run decided about it."""

    run_id: str
    checked_at: datetime
    text: str
    outcome: Outcome
    pending_alert: str | None = None


class Verdict(BaseModel):
    important: bool = Field(description="Whether the reader should hear about this change.")
    summary: str = Field(description="One or two plain sentences on what changed.")


class CheckResult(BaseModel):
    watch_id: str
    url: str
    outcome: Outcome


class SweepReport(BaseModel):
    run_id: str
    results: list[CheckResult]
    failed: list[str]
