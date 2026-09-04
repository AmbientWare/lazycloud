from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from database.records.autoscaling import AutoscalingTargetClaim
from database.repositories.orchestration import AutoscalingTargetRepository

from database import DatabaseClient


class AutoscalingTargetContext(Protocol):
    @property
    def database(self) -> DatabaseClient: ...


@dataclass(frozen=True, slots=True)
class AutoscalingTargetService:
    context: AutoscalingTargetContext

    def claim_due(
        self,
        *,
        now: datetime,
        limit: int,
        lease_seconds: float,
    ) -> list[AutoscalingTargetClaim]:
        with self.context.database.session() as session:
            return AutoscalingTargetRepository(session).claim_due(
                now=now,
                limit=limit,
                lease_seconds=lease_seconds,
            )

    def complete_many(
        self,
        completions: Sequence[tuple[AutoscalingTargetClaim, datetime | None]],
        *,
        now: datetime,
    ) -> int:
        with self.context.database.session() as session:
            return AutoscalingTargetRepository(session).complete_many(completions, now=now)


__all__ = ["AutoscalingTargetService"]
