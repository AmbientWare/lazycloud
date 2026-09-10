from uuid import uuid4

import pytest
from database.context import ServiceContext
from database.records.apps import AppRecord
from database.repositories.apps import AppRepository
from shared.app_lifecycle import AppLifecycleState
from shared.timestamps import utc_now
from sqlalchemy.exc import IntegrityError


def test_active_app_names_are_unique_per_workspace(service_context: ServiceContext) -> None:
    with service_context.database.session() as session:
        workspace_id = service_context.default_workspace_id(session)
        AppRepository(session).upsert(
            AppRecord(id=str(uuid4()), workspace_id=workspace_id, name="api")
        )

    with pytest.raises(IntegrityError), service_context.database.session() as session:
        AppRepository(session).upsert(
            AppRecord(id=str(uuid4()), workspace_id=workspace_id, name="api")
        )

    with service_context.database.session() as session:
        deleted = AppRepository(session).upsert(
            AppRecord(
                id=str(uuid4()),
                workspace_id=workspace_id,
                name="api",
                lifecycle_state=AppLifecycleState.Deleted,
                deleted_at=utc_now(),
            )
        )
        assert deleted.name == "api"
        assert deleted.deleted_at is not None
