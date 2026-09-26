from datetime import datetime, timedelta
from typing import Literal

from database.repositories.capacity_activations import (
    CapacityActivationRepository,
    CapacityActivationSummary,
)
from database.repositories.startup_latency import (
    FunctionExecutionFacts,
    FunctionStartupFacts,
    StartupLatencyRepository,
)
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
    execution: FunctionExecutionFacts
    platform_activations: tuple[CapacityActivationSummary, ...]
    warm_execution_measurement: Literal["server_clock_upper_bound"] = "server_clock_upper_bound"
    limitations: tuple[str, ...] = (
        "Function readiness is the first runner claim poll after initialization.",
        "Container creation is the earliest recorded timestamp; preceding API work is excluded.",
        "Execution-entry bounds subtract runner monotonic elapsed time from server receipt; "
        "transport delay remains included.",
        "Entry evidence is delivered when the handler returns or raises; "
        "killed and unfinished calls can lack evidence.",
        "Warm classification requires readiness before the invocation became claimable; "
        "retries include retry delay.",
        "Images with an older SDK may lack decorated-handler entry instrumentation.",
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
            execution = StartupLatencyRepository(session).execution_entries(
                workspace_id=workspace_id, since=since, now=until
            )
            activations = CapacityActivationRepository(session).summarize(since=since)
        return StartupLatencyReport(
            workspace_id=workspace_id,
            since=since,
            until=until,
            functions=facts,
            execution=execution,
            platform_activations=activations,
        )
