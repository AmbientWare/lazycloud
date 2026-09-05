from __future__ import annotations

from uuid import uuid4

from database.records.apps import StubRecord
from database.repositories.apps import StubRepository
from database.repositories.container_rollouts import ContainerRolloutRepository
from database.repositories.execution import TaskRepository
from database.repositories.identity import WorkspaceRepository
from database.repositories.orchestration import ContainerRepository
from shared.containers import ContainerRecord, ContainerStatus
from shared.tasks import Task
from shared.timestamps import utc_now
from sqlalchemy.engine import URL

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings


def test_rollout_admission_preserves_claimed_work_and_bounds_replacement_capacity(
    postgres_database_url: URL,
) -> None:
    database = DatabaseClient.from_settings(
        DatabaseSettings(
            url=postgres_database_url.render_as_string(hide_password=False),
            application_name=DatabaseApplicationName.Test,
        )
    )
    try:
        database.create_schema()
        now = utc_now()
        with database.session() as session:
            workspace = WorkspaceRepository(session).create(name="rollout-admission")
            stub = StubRepository(session).upsert(
                StubRecord(
                    id=str(uuid4()),
                    workspace_id=workspace.id,
                    name="work",
                )
            )
            old = ContainerRecord(
                id=str(uuid4()),
                name="old",
                image="image",
                command=[],
                workspace_id=workspace.id,
                stub_id=stub.id,
                status=ContainerStatus.Running,
            )
            ContainerRepository(session).records.upsert(
                old,
                workspace_id=workspace.id,
                name=old.name,
                status=old.status.value,
            )
            task = TaskRepository(session).upsert(
                Task(
                    id=str(uuid4()),
                    name="first",
                    workspace_id=workspace.id,
                    stub_id=stub.id,
                    claimable_at=now,
                )
            )
            rollouts = ContainerRolloutRepository(session)
            rollouts.prepare(old, serving_floor=1, now=now)
            assert rollouts.accepting_work(old.id, stub_id=stub.id)
            assert ContainerRepository(session).count_live_for_stub(stub.id) == 0
            [claimed] = TaskRepository(session).claim_for_stub(
                stub.id, container_id=old.id, limit=1
            )
            assert claimed.id == task.id
            assert rollouts.ready_container_ids([old.id]) == {old.id}
        with database.session() as session:
            replacement = old.model_copy(update={"id": str(uuid4()), "name": "replacement"})
            ContainerRepository(session).records.upsert(
                replacement,
                workspace_id=workspace.id,
                name=replacement.name,
                status=replacement.status.value,
            )
            assert ContainerRepository(session).count_live_for_stub(stub.id) == 1
            rollouts = ContainerRolloutRepository(session)
            assert rollouts.close_admission(old.id, now=now)
            assert not rollouts.accepting_work(old.id, stub_id=stub.id)
            assert not rollouts.accepting_work(replacement.id, stub_id=str(uuid4()))
            TaskRepository(session).upsert(
                Task(
                    id=str(uuid4()),
                    name="next",
                    workspace_id=workspace.id,
                    stub_id=stub.id,
                    claimable_at=now,
                )
            )
            assert (
                TaskRepository(session).claim_for_stub(stub.id, container_id=old.id, limit=1) == []
            )
            assert TaskRepository(session).containers_with_inflight_work([old.id]) == {old.id}
            [next_task] = TaskRepository(session).claim_for_stub(
                stub.id, container_id=replacement.id, limit=1
            )
            assert next_task.id != claimed.id
            assert rollouts.closed_for_stub(stub.id) == {old.id}
    finally:
        database.dispose()
