from datetime import datetime, timedelta
from typing import Literal

from database.repositories.startup_latency import FunctionStartupFacts, StartupLatencyRepository
from shared.contracts import ContractModel
from shared.errors import InvalidInputError
from shared.startup import (
    COLD_CONTAINER_START_TARGET_SECONDS,
    WARM_EXECUTION_START_TARGET_SECONDS,
)
from shared.timestamps import to_utc, utc_now

from database import DatabaseClient


class StartupLatencyReport(ContractModel):
    workspace_id: str
    since: datetime
    until: datetime
    cold_container_target_seconds: float = COLD_CONTAINER_START_TARGET_SECONDS
    warm_execution_target_seconds: float = WARM_EXECUTION_START_TARGET_SECONDS
    functions: FunctionStartupFacts
    warm_execution_measurement: Literal["unavailable"] = "unavailable"
    limitations: tuple[str, ...] = (
        "Function readiness is the first runner claim poll after initialization.",
        "Container creation is the earliest recorded timestamp; preceding API work is excluded.",
        "Task and endpoint start timestamps record server claims, not user execution start.",
        "Other workload kinds do not persist application readiness timestamps.",
    )


class StartupLatencyService:
    def __init__(self, database: DatabaseClient) -> None:
        self.database = database

    def read(
        self,
        *,
        workspace_id: str,
        window_seconds: int = 3600,
        now: datetime | None = None,
    ) -> StartupLatencyReport:
        if not 1 <= window_seconds <= 86400:
            raise InvalidInputError("startup latency window must be between 1 and 86400 seconds")
        until = to_utc(now or utc_now())
        since = until - timedelta(seconds=window_seconds)
        with self.database.session() as session:
            facts = StartupLatencyRepository(session).functions(
                workspace_id=workspace_id,
                since=since,
                now=until,
                deadline_seconds=COLD_CONTAINER_START_TARGET_SECONDS,
            )
        return StartupLatencyReport(
            workspace_id=workspace_id, since=since, until=until, functions=facts
        )
