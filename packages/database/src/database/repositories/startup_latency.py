from dataclasses import dataclass
from datetime import datetime

from database.tables.apps import StubTable
from database.tables.orchestration import ContainerTable
from shared.contracts import ContractModel
from shared.deployments import StubKind
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session


class StartupLatencyDistribution(ContractModel):
    observed: int
    p50_seconds: float | None
    p95_seconds: float | None
    p99_seconds: float | None


class FunctionStartupFacts(ContractModel):
    requested: int
    ready: int
    overdue_without_readiness: int
    failed_before_readiness: int
    ready_after_deadline: int
    request_to_ready: StartupLatencyDistribution
    scheduling_to_assignment: StartupLatencyDistribution
    assignment_to_runtime: StartupLatencyDistribution
    runtime_to_ready: StartupLatencyDistribution


@dataclass(slots=True)
class StartupLatencyRepository:
    session: Session

    def functions(
        self,
        *,
        workspace_id: str,
        since: datetime,
        now: datetime,
        deadline_seconds: float,
    ) -> FunctionStartupFacts:
        container = ContainerTable
        # Only functions persist workload readiness. Runtime start alone does not
        # establish application readiness for other workload kinds.
        cohort = (
            select(
                container.created_at,
                container.workload_ready_at,
                container.scheduling_requested_at,
                container.scheduling_assigned_at,
                container.started_at,
                container.status,
            )
            .join(StubTable, StubTable.id == container.stub_id)
            .where(
                container.workspace_id == workspace_id,
                container.created_at >= since,
                container.created_at < now,
                StubTable.type == StubKind.Function.value,
            )
            .cte("startup_cohort")
        )
        ready = cohort.c.workload_ready_at <= now
        unready = ~func.coalesce(ready, False)
        ready_seconds = func.extract("epoch", cohort.c.workload_ready_at - cohort.c.created_at)
        statement = select(
            func.count().label("requested"),
            func.count().filter(ready).label("ready"),
            func.count()
            .filter(
                unready,
                func.extract("epoch", now - cohort.c.created_at) >= deadline_seconds,
            )
            .label("overdue_without_readiness"),
            func.count()
            .filter(unready, cohort.c.status.in_(("failed", "exited", "stopped")))
            .label("failed_before_readiness"),
            func.count()
            .filter(ready, ready_seconds >= deadline_seconds)
            .label("ready_after_deadline"),
        ).select_from(cohort)
        for name, start, end in (
            ("request", cohort.c.created_at, cohort.c.workload_ready_at),
            ("scheduling", cohort.c.scheduling_requested_at, cohort.c.scheduling_assigned_at),
            ("runtime", cohort.c.scheduling_assigned_at, cohort.c.started_at),
            ("ready", cohort.c.started_at, cohort.c.workload_ready_at),
        ):
            seconds = func.extract("epoch", end - start)
            observed = and_(start.is_not(None), end <= now, seconds >= 0)
            statement = statement.add_columns(
                func.count().filter(observed).label(f"{name}_observed"),
                *(
                    func.percentile_cont(percentile / 100)
                    .within_group(seconds)
                    .filter(observed)
                    .label(f"{name}_p{percentile}_seconds")
                    for percentile in (50, 95, 99)
                ),
            )
        row = self.session.execute(statement).one()
        request, scheduling, runtime, ready_delay = (
            StartupLatencyDistribution.model_validate(
                {
                    field: row._mapping[f"{name}_{field}"]
                    for field in StartupLatencyDistribution.model_fields
                }
            )
            for name in ("request", "scheduling", "runtime", "ready")
        )
        return FunctionStartupFacts(
            requested=row.requested,
            ready=row.ready,
            overdue_without_readiness=row.overdue_without_readiness,
            failed_before_readiness=row.failed_before_readiness,
            ready_after_deadline=row.ready_after_deadline,
            request_to_ready=request,
            scheduling_to_assignment=scheduling,
            assignment_to_runtime=runtime,
            runtime_to_ready=ready_delay,
        )
