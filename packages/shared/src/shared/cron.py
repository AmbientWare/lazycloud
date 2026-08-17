from __future__ import annotations

import re
from datetime import datetime, timezone

from croniter import croniter
from pydantic import Field, JsonValue

from shared.contracts import ContractModel
from shared.timestamps import utc_now

CRON_ALIASES = frozenset(
    {
        "@annually",
        "@daily",
        "@hourly",
        "@midnight",
        "@monthly",
        "@weekly",
        "@yearly",
    }
)
INTERVAL_PATTERN = re.compile(r"^every\s+([1-9][0-9]*)([mhd])$")


def normalize_cron_expression(expression: str) -> str:
    normalized = " ".join(expression.strip().lower().split())
    if not normalized:
        raise ValueError("cron expression is required")
    interval = INTERVAL_PATTERN.fullmatch(normalized)
    if interval is not None:
        value = int(interval.group(1))
        unit = interval.group(2)
        if unit == "m" and value <= 59:
            normalized = f"*/{value} * * * *"
        elif unit == "h" and value <= 23:
            normalized = f"0 */{value} * * *"
        elif unit == "d" and value <= 31:
            normalized = f"0 0 */{value} * *"
        else:
            raise ValueError(f"unsupported cron interval: {expression}")
    if normalized.startswith("@"):
        if normalized not in CRON_ALIASES:
            raise ValueError(f"unsupported cron alias: {normalized}")
    elif len(normalized.split()) != 5:
        raise ValueError("cron expression must contain exactly five fields")
    if not croniter.is_valid(normalized):
        raise ValueError(f"invalid cron expression: {expression}")
    return normalized


def next_cron_run(expression: str, after: datetime | None = None) -> datetime:
    normalized = normalize_cron_expression(expression)
    base = after or datetime.now(timezone.utc)
    base = (
        base.replace(tzinfo=timezone.utc) if base.tzinfo is None else base.astimezone(timezone.utc)
    )
    next_run = croniter(normalized, base).get_next(datetime)
    if next_run.tzinfo is None:
        return next_run.replace(tzinfo=timezone.utc)
    return next_run.astimezone(timezone.utc)


class CronJobRun(ContractModel):
    id: str
    workspace_id: str
    cron_job: str
    enqueued: bool
    message_id: str | None = None
    task_id: str | None = None
    reason: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class CronJobRecord(ContractModel):
    workspace_id: str
    name: str
    cron: str
    deployment_id: str
    queue: str = "tasks"
    payload: JsonValue = None
    enabled: bool = True
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


def schedule_payload(
    *,
    stub_id: str,
    workspace_name: str,
    deployment_id: str,
    cron: str,
) -> dict[str, JsonValue]:
    """What the scheduler reads back when the schedule fires.

    The stub id is the load-bearing part: a schedule names a deployment, and the
    tick has to reach the stub that deployment published to invoke it.
    """

    return {
        "stub_id": stub_id,
        "workspace_name": workspace_name,
        "deployment_id": deployment_id,
        "cron": cron,
    }


__all__ = [
    "CRON_ALIASES",
    "CronJobRecord",
    "CronJobRun",
    "next_cron_run",
    "normalize_cron_expression",
    "schedule_payload",
]
