from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from coordination.redis_client import RedisClient
from coordination.token_lock import release_token_lock, try_acquire_token_lock
from database.records.apps import StubRecord
from database.repositories.apps import CronJobRepository
from database.repositories.execution import CronJobRunCursor, CronJobRunRepository
from shared.contracts import ContractModel
from shared.cron import CronJobRecord, CronJobRun, next_cron_run
from shared.errors import InvalidInputError
from shared.function_payloads import FunctionJsonInvocation, FunctionPayloadEncoding
from shared.http.functions import FunctionInvokeBody, FunctionInvokeResponse
from shared.http.workspace_changes import WorkspaceChangeType
from shared.timestamps import utc_now

from scheduler.services import SchedulerServices


class CronFunctionInvoker(Protocol):
    def function_invoke(
        self, request: FunctionInvokeBody, *, stub: StubRecord
    ) -> FunctionInvokeResponse: ...


@dataclass(slots=True)
class CronScheduler:
    services: SchedulerServices
    redis: RedisClient
    functions: CronFunctionInvoker

    def tick(self, now: datetime | None = None, *, limit: int = 100) -> list[CronJobRun]:
        current = (now or utc_now()).astimezone(UTC)
        with self.services.context.database.session() as session:
            due = CronJobRepository(session).due_across_workspaces(now=current, limit=max(limit, 0))
        return [self._run(cron_job, current) for cron_job in due]

    def _run(self, cron_job: CronJobRecord, now: datetime) -> CronJobRun:
        run = CronJobRun(
            id=str(uuid4()),
            workspace_id=cron_job.workspace_id,
            cron_job=cron_job.name,
            enqueued=False,
        )
        try:
            deployment = self.services.deployments.get(cron_job.deployment_id)
            if not deployment.active:
                run.reason = "deployment inactive"
            elif not deployment.stub_id:
                raise ValueError("scheduled deployment published no stub to invoke")
            else:
                stub_id = deployment.stub_id
                key = self.redis.key(f"function:cron_jobs_lock:{stub_id}")
                token = uuid4().hex
                if not try_acquire_token_lock(self.redis, key, token, ttl_seconds=10):
                    run.reason = "cron job lock not acquired"
                else:
                    try:
                        stub = self.services.control_plane_service.get_stub(
                            stub_id, workspace=cron_job.workspace_id
                        )
                        response = self.functions.function_invoke(
                            FunctionInvokeBody(
                                stub_id=stub_id,
                                invocation=FunctionJsonInvocation(
                                    result_encoding=FunctionPayloadEncoding.Cloudpickle
                                ),
                                headless=True,
                            ),
                            stub=stub,
                        )
                        run.enqueued = bool(response.task_id and response.exit_code == 0)
                        run.task_id = response.task_id or None
                        if not run.enqueued:
                            run.reason = response.output or "cron function invocation failed"
                    finally:
                        release_token_lock(self.redis, key, token)
        except Exception as exc:
            run.enqueued = False
            run.task_id = None
            run.reason = str(exc)

        cron_job.last_run_at = now
        cron_job.next_run_at = next_cron_run(cron_job.cron, now)
        cron_job.updated_at = utc_now()
        run.created_at = utc_now()
        with self.services.context.database.session() as session:
            updated = CronJobRepository(session).record_run(
                cron_job, workspace_id=cron_job.workspace_id
            )
            saved = CronJobRunRepository(session).append(run)
        if updated:
            self.services.cron_jobs.publish_change(cron_job, WorkspaceChangeType.Updated)
        return saved

    def list_cron_job_runs(
        self,
        *,
        workspace_id: str,
        limit: int = 100,
        cursor: str | None = None,
    ) -> SchedulerCronJobRunPage:
        with self.services.context.database.session() as session:
            page = CronJobRunRepository(session).page(
                workspace_id=workspace_id,
                cursor=_decode_cron_job_run_cursor(cursor),
                limit=min(max(limit, 1), 1_000),
            )
        return SchedulerCronJobRunPage(
            data=page.data,
            next=_encode_cron_job_run_cursor(page.next),
        )


class CronJobRunCursorPayload(ContractModel):
    created_at: datetime
    id: str


@dataclass(frozen=True, slots=True)
class SchedulerCronJobRunPage:
    data: tuple[CronJobRun, ...]
    next: str = ""


def _encode_cron_job_run_cursor(cursor: CronJobRunCursor | None) -> str:
    if cursor is None:
        return ""
    payload = CronJobRunCursorPayload(
        created_at=cursor.created_at,
        id=cursor.id,
    ).model_dump_json()
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _decode_cron_job_run_cursor(value: str | None) -> CronJobRunCursor | None:
    if not value:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = CronJobRunCursorPayload.model_validate_json(
            base64.urlsafe_b64decode(padded.encode())
        )
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise InvalidInputError("invalid cron job run cursor") from exc
    return CronJobRunCursor(created_at=payload.created_at, id=payload.id)
