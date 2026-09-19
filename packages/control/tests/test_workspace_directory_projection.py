from __future__ import annotations

from control.service import ControlPlaneService
from database.context import ServiceContext
from database.repositories.identity import WorkspaceRepository
from shared.identity import WorkspaceStatus
from tests.workspaces import owned_workspace


def test_workspace_directory_projects_active_deleting_and_deleted_lifecycles(
    service_context: ServiceContext,
) -> None:
    control = ControlPlaneService(service_context)
    active = owned_workspace(control, "projection-active")
    deleting = owned_workspace(control, "projection-deleting")
    deleted = owned_workspace(control, "projection-deleted")

    with service_context.database.session() as session:
        repository = WorkspaceRepository(session)
        deleting_record = repository.get(deleting.id)
        deleted_record = repository.get(deleted.id)
        assert deleting_record is not None
        assert deleted_record is not None
        repository.mark_deleting(deleting_record)
        repository.tombstone(repository.mark_deleting(deleted_record))

    assert _projection(control) == {
        active.name: WorkspaceStatus.Active,
    }
    assert _projection(control, include_deleting=True) == {
        active.name: WorkspaceStatus.Active,
        deleting.name: WorkspaceStatus.Deleting,
    }
    assert _projection(control, include_deleted=True) == {
        active.name: WorkspaceStatus.Active,
        deleted.name: WorkspaceStatus.Deleted,
        deleting.name: WorkspaceStatus.Deleting,
    }


def _projection(
    control: ControlPlaneService,
    *,
    include_deleting: bool = False,
    include_deleted: bool = False,
) -> dict[str, WorkspaceStatus]:
    return {
        workspace.name: workspace.status
        for workspace in control.list_workspaces(
            include_deleting=include_deleting,
            include_deleted=include_deleted,
        )
        if workspace.name.startswith("projection-")
    }
