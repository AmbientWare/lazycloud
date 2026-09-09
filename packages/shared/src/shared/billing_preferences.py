from dataclasses import dataclass
from datetime import datetime

from shared.enums import StringEnum
from shared.timestamps import to_utc


class AutomaticReloadPauseReason(StringEnum):
    Declined = "declined"
    ActionRequired = "action_required"


@dataclass(frozen=True, slots=True)
class UsageBudget:
    month_started_at: datetime
    month_ended_at: datetime
    limit_nanos: int | None
    spent_nanos: int
    available_nanos: int | None


def usage_budget_month(at: datetime) -> tuple[datetime, datetime]:
    start = to_utc(at).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start, end


__all__ = ["AutomaticReloadPauseReason", "UsageBudget", "usage_budget_month"]
