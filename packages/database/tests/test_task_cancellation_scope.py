from __future__ import annotations

from uuid import uuid4

from control.service import ControlPlaneService
from database.context import ServiceContext
from database.records.apps import AppRecord
from database.repositories.apps import AppRepository
from database.repositories.execution import TaskRepository
from database.repositories.orchestration import ContainerRepository
from shared.containers import ContainerRecord
from shared.tasks import Task, TaskStatus
from tests.domain_fixtures import owned_workspace


def test_deleting_an_app_retires_its_queued_work_and_nothing_else(
    service_context: ServiceContext,
) -> None:
    """Cancellation stops at the queue of the app being deleted.

    Every row this reaches wrongly is somebody's work destroyed, and the three
    ways to get it wrong are all one predicate away: another app's backlog, a
    task a container is already running, and a task that finished long ago.
    """

    control = ControlPlaneService(service_context)
    workspace = owned_workspace(control, "task-cancellation")
    doomed_app = str(uuid4())
    other_app = str(uuid4())
    container_id = str(uuid4())

    with service_context.database.session() as session:
        apps = AppRepository(session)
        apps.upsert(AppRecord(id=doomed_app, workspace_id=workspace.id, name="doomed"))
        apps.upsert(AppRecord(id=other_app, workspace_id=workspace.id, name="other"))
        ContainerRepository(session).upsert(
            ContainerRecord(
                id=container_id,
                name="running-container",
                image="registry/image:1",
                command=["sleep"],
                workspace_id=workspace.id,
            )
        )
        tasks = TaskRepository(session)
        for name, app_id, status, held_by in (
            ("queued", doomed_app, TaskStatus.Pending, None),
            # Holding a container, the way a task that failed an attempt does.
            ("awaiting-retry", doomed_app, TaskStatus.Retry, container_id),
            ("running", doomed_app, TaskStatus.Running, container_id),
            ("already-done", doomed_app, TaskStatus.Complete, None),
            ("someone-elses", other_app, TaskStatus.Pending, None),
        ):
            tasks.upsert(
                Task(
                    id=str(uuid4()),
                    name=name,
                    status=status,
                    workspace_id=workspace.id,
                    app_id=app_id,
                    container_id=held_by,
                ),
                workspace_id=workspace.id,
            )

    with service_context.database.session() as session:
        cancelled = TaskRepository(session).cancel_queued_for_app(
            workspace_id=workspace.id,
            app_id=doomed_app,
            error="the app this task belongs to was deleted",
        )

    assert {task.name for task in cancelled} == {"queued", "awaiting-retry"}
    assert all(task.status is TaskStatus.Cancelled for task in cancelled)
    assert all(task.finished_at is not None for task in cancelled)

    with service_context.database.session() as session:
        surviving = {
            task.name: task.status
            for task in TaskRepository(session).list(workspace_id=workspace.id)
        }
    assert surviving["running"] is TaskStatus.Running
    assert surviving["already-done"] is TaskStatus.Complete
    assert surviving["someone-elses"] is TaskStatus.Pending
