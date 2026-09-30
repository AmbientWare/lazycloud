from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from control.cron_jobs import CronJobService
from database.records.apps import StubRecord
from database.repositories.cron_jobs import CronJobRepository
from database.repositories.execution import CronJobRunRepository
from database.repositories.identity import WorkspaceRepository
from execution.functions.service import FunctionAdmission, FunctionControlService
from shared.cron import CronJobRecord, CronJobRun
from shared.errors import (
    DomainError,
    InvalidInputError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
)
from shared.function_payloads import FunctionJsonInvocation, FunctionPayloadEncoding
from shared.http.functions import FunctionInvokeBody
from shared.http.workspace_changes import WorkspaceChangeType
from shared.tasks import Task
from shared.timestamps import utc_now
from shared.usage import UsageRecord

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class CronScheduler:
    schedules: CronJobService
    functions: FunctionControlService

    def tick(self, now: datetime | None = None, *, limit: int = 100) -> list[CronJobRun]:
        current = (now or utc_now()).astimezone(UTC)
        with self.schedules.context.database.session() as session:
            due = CronJobRepository(session).due_across_workspaces(now=current, limit=max(limit, 0))
        runs: list[CronJobRun] = []
        failure: Exception | None = None
        for job, stub in due:
            try:
                run = self._run(job, stub, current)
                if run is not None:
                    runs.append(run)
            except Exception as exc:
                LOGGER.exception("cron pass failed for schedule %s", job.name)
                failure = exc
        if failure is not None:
            raise failure
        return runs

    def _run(self, job: CronJobRecord, stub: StubRecord | None, now: datetime) -> CronJobRun | None:
        try:
            if stub is None:
                raise InvalidInputError("scheduled deployment published no stub to invoke")
            admission = self.functions.prepare_admission(
                [
                    FunctionInvokeBody(
                        stub_id=stub.id,
                        invocation=FunctionJsonInvocation(
                            result_encoding=FunctionPayloadEncoding.Cloudpickle
                        ),
                        headless=True,
                    )
                ],
                stub=stub,
            )
            if not admission.prepared:
                result = admission.results[0]
                if isinstance(result, Exception):
                    raise result
                raise RuntimeError("cron admission omitted its task")
            return self._commit(job, now, admission=admission)
        except (UpstreamUnavailableError, UpstreamTimeoutError):
            raise
        except DomainError as exc:
            return self._commit(job, now, reason=str(exc))

    def _commit(
        self,
        job: CronJobRecord,
        now: datetime,
        *,
        admission: FunctionAdmission | None = None,
        reason: str | None = None,
    ) -> CronJobRun | None:
        tasks: list[Task] = []
        usage: list[UsageRecord] = []
        with self.schedules.context.database.session() as session:
            WorkspaceRepository(session).lock_active_owner(job.workspace_id)
            if admission is not None:
                self.functions.lock_admission(session, admission)
            if not self.schedules.advance_in_session(session, job, now=now):
                return None
            if admission is not None:
                tasks, usage = self.functions.persist_admission(session, admission)
            run = CronJobRunRepository(session).append(
                CronJobRun(
                    id=str(uuid4()),
                    workspace_id=job.workspace_id,
                    cron_job=job.name,
                    schedule_revision=job.revision,
                    scheduled_at=job.next_run_at,
                    enqueued=bool(tasks),
                    task_id=tasks[0].id if tasks else None,
                    reason=reason,
                )
            )
        if tasks:
            self.functions.publish_admission(tasks, usage)
        try:
            self.schedules.publish_change(job, WorkspaceChangeType.Updated)
        except Exception:
            LOGGER.exception(
                "cron occurrence committed; schedule notification failed for %s", job.name
            )
        return run
