from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

from database.repositories.identity import WorkspaceRepository
from database.repositories.orchestration import AutoscalerStateRepository
from shared.autoscaler_state import AutoscalerStateRecord, AutoscalerTargetKind
from sqlalchemy.engine import URL

from database import (
    DatabaseApplicationName,
    DatabaseClient,
    DatabaseSettings,
)


def test_postgresql_concurrent_autoscaler_upsert_keeps_one_state(
    migrated_database_url: URL,
) -> None:
    database_url = migrated_database_url.render_as_string(hide_password=False)
    database = DatabaseClient.from_settings(
        DatabaseSettings(
            url=database_url,
            pool_size=2,
            application_name=DatabaseApplicationName.Test,
        )
    )
    with database.session() as session:
        workspace = WorkspaceRepository(session).create(name=f"autoscaler-{uuid4()}")
    target_id = str(uuid4())
    barrier = Barrier(2)

    def write(decision: str) -> AutoscalerStateRecord:
        barrier.wait()
        with database.session() as session:
            return AutoscalerStateRepository(session).upsert(
                AutoscalerStateRecord(
                    name=f"function:{target_id}",
                    workspace_id=workspace.id,
                    source="function.autoscaler",
                    target_kind=AutoscalerTargetKind.Function,
                    target_id=target_id,
                    decision=decision,
                    reason=decision,
                )
            )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(write, ("hold", "scale-up")))
        with database.session() as session:
            rows = AutoscalerStateRepository(session).list(workspace_id=workspace.id)
    finally:
        database.dispose()

    assert len(results) == 2
    assert len(rows) == 1
    assert rows[0].decision in {"hold", "scale-up"}
    assert rows[0].reason == rows[0].decision
