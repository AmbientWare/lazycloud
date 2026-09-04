from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from control.service import ControlPlaneService
from database.context import ServiceContext
from database.repositories.identity import WorkspaceRepository
from database.repositories.orchestration import AutoscalingTargetRepository
from shared.autoscaler_state import AutoscalerTargetKind
from shared.timestamps import utc_now
from sqlalchemy.engine import URL

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings


def test_activation_during_claim_is_not_lost(
    postgres_database_url: URL,
    tmp_path: Path,
) -> None:
    database = DatabaseClient.from_settings(
        DatabaseSettings(
            url=postgres_database_url.render_as_string(hide_password=False),
            application_name=DatabaseApplicationName.Test,
        )
    )
    context = ServiceContext.create(database, root=tmp_path, create_schema=True)
    try:
        with database.session() as session:
            workspace = WorkspaceRepository(session).create(name="target-owner")
        stub = ControlPlaneService(context).create_stub(
            "claimed-target",
            workspace=workspace.id,
        )
        claim_time = utc_now() + timedelta(seconds=1)

        with database.session() as session:
            [claim] = AutoscalingTargetRepository(session).claim_due(
                now=claim_time,
                limit=1,
                lease_seconds=30,
            )

        with database.session() as session:
            AutoscalingTargetRepository(session).activate(
                stub_id=stub.id,
                workspace_id=stub.workspace_id,
                target_kind=AutoscalerTargetKind.Function,
                due_at=claim_time,
            )

        with database.session() as session:
            assert AutoscalingTargetRepository(session).complete(
                claim,
                next_reconcile_at=None,
                now=claim_time,
            )

        with database.session() as session:
            [reactivated] = AutoscalingTargetRepository(session).claim_due(
                now=claim_time,
                limit=1,
                lease_seconds=30,
            )
            assert reactivated.generation == claim.generation + 1
            assert AutoscalingTargetRepository(session).complete(
                reactivated,
                next_reconcile_at=None,
                now=claim_time,
            )

        with database.session() as session:
            assert (
                AutoscalingTargetRepository(session).claim_due(
                    now=claim_time,
                    limit=1,
                    lease_seconds=30,
                )
                == []
            )
    finally:
        database.dispose()
