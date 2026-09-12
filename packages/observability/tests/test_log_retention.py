from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from database.context import ServiceContext
from database.repositories.billing import BillingAccountRepository
from database.repositories.execution import LogRepository, TaskRepository
from database.tables.execution import LogTable
from observability.log_retention import LogRetentionService
from shared.logs import LogEntry
from shared.tasks import Task
from shared.timestamps import utc_now
from sqlalchemy import select, update
from tests.workspaces import unbilled_account


def test_complimentary_log_retention_preserves_business_history(
    service_context: ServiceContext,
) -> None:
    user_id, workspace_id = unbilled_account(service_context)
    now = utc_now()
    recent_id, expired_id = str(uuid4()), str(uuid4())
    with service_context.database.session() as session:
        accounts = BillingAccountRepository(session)
        accounts.lock_for_registration(user_id)
        accounts.set_complimentary(user_id=user_id, present=True, at=now)
        task_id = str(uuid4())
        TaskRepository(session).upsert(Task(id=task_id, name="logs", workspace_id=workspace_id))
        logs = LogRepository(session)
        for log_id, days in ((recent_id, 60), (expired_id, 91)):
            logs.append(
                LogEntry(
                    id=log_id,
                    task_id=task_id,
                    message="output",
                ),
                workspace_id=workspace_id,
            )
            session.execute(
                update(LogTable)
                .where(LogTable.id == log_id)
                .values(created_at=now - timedelta(days=days))
            )

    retention = LogRetentionService(service_context)
    assert retention.cutoff(workspace_id, now=now) == now - timedelta(days=90)
    assert retention.prune(now=now) == 1
    with service_context.database.session() as session:
        assert list(
            session.scalars(select(LogTable.id).where(LogTable.workspace_id == workspace_id))
        ) == [recent_id]
