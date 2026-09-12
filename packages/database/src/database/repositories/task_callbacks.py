from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from database.tables.task_callbacks import TaskCallbackTable
from pydantic import JsonValue
from shared.http.callbacks import TaskCallbackBody
from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class ClaimedTaskCallback:
    id: str
    task_id: str
    workspace_id: str
    target: str
    body: TaskCallbackBody
    idempotency_key: str
    claim_token: str
    attempts: int


@dataclass(slots=True)
class TaskCallbackRepository:
    session: Session

    def enqueue(
        self,
        *,
        task_id: str,
        workspace_id: str,
        target: str,
        payload: dict[str, JsonValue],
        idempotency_key: str,
        now: datetime,
    ) -> None:
        self.session.execute(
            insert(TaskCallbackTable)
            .values(
                task_id=task_id,
                workspace_id=workspace_id,
                target=target,
                payload=payload,
                idempotency_key=idempotency_key,
                status="pending",
                attempts=0,
                next_attempt_at=now,
            )
            .on_conflict_do_nothing(index_elements=[TaskCallbackTable.idempotency_key])
        )

    def claim(
        self,
        *,
        now: datetime,
        stale_before: datetime,
        max_attempts: int,
    ) -> ClaimedTaskCallback | None:
        exhausted = self.session.scalars(
            select(TaskCallbackTable)
            .where(
                TaskCallbackTable.status == "sending",
                TaskCallbackTable.claimed_at <= stale_before,
                TaskCallbackTable.attempts >= max_attempts,
            )
            .limit(100)
            .with_for_update(skip_locked=True)
        ).all()
        for row in exhausted:
            row.status = "failed"
            row.claim_token = None
            row.target = ""
            row.payload = {}
            row.updated_at = now
        row = self.session.scalar(
            select(TaskCallbackTable)
            .where(
                TaskCallbackTable.attempts < max_attempts,
                or_(
                    and_(
                        TaskCallbackTable.status == "pending",
                        TaskCallbackTable.next_attempt_at <= now,
                    ),
                    and_(
                        TaskCallbackTable.status == "sending",
                        TaskCallbackTable.claimed_at <= stale_before,
                    ),
                ),
            )
            .order_by(TaskCallbackTable.next_attempt_at, TaskCallbackTable.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if row is None:
            return None
        claim_token = uuid4().hex
        row.status = "sending"
        row.claim_token = claim_token
        row.claimed_at = now
        row.updated_at = now
        row.attempts += 1
        return ClaimedTaskCallback(
            id=row.id,
            task_id=row.task_id,
            workspace_id=row.workspace_id,
            target=row.target,
            body=TaskCallbackBody.model_validate(row.payload),
            idempotency_key=row.idempotency_key,
            claim_token=claim_token,
            attempts=row.attempts,
        )

    def settle(
        self,
        claimed: ClaimedTaskCallback,
        *,
        now: datetime,
        succeeded: bool,
        retry_at: datetime | None = None,
    ) -> bool:
        values: dict[str, str | datetime | dict[str, JsonValue] | None] = {
            "status": "pending" if retry_at is not None else "sent" if succeeded else "failed",
            "claim_token": None,
            "updated_at": now,
        }
        if retry_at is not None:
            values["next_attempt_at"] = retry_at
        else:
            values["target"] = ""
            values["payload"] = {}
        return (
            self.session.scalar(
                update(TaskCallbackTable)
                .where(
                    TaskCallbackTable.id == claimed.id,
                    TaskCallbackTable.status == "sending",
                    TaskCallbackTable.claim_token == claimed.claim_token,
                )
                .values(**values)
                .returning(TaskCallbackTable.id)
            )
            is not None
        )
