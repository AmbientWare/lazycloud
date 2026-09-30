from __future__ import annotations

from dataclasses import dataclass

from database.records.apps import AppRecord
from database.repositories.apps import AppRepository, DeploymentRepository
from foundation.ids import try_uuid
from shared.deployment_records import Deployment
from shared.errors import NotFoundError
from sqlalchemy.orm import Session

from control.context import ControlContext


@dataclass(frozen=True, slots=True)
class DatabaseAppReader:
    context: ControlContext

    def get(self, app_id_or_name: str, *, workspace: str | None = None) -> AppRecord:
        with self.context.database.session() as session:
            return self.get_in_session(session, app_id_or_name, workspace=workspace)

    def get_in_session(
        self,
        session: Session,
        app_id_or_name: str,
        *,
        workspace: str | None = None,
    ) -> AppRecord:
        workspace_id = (
            self.context.workspace(session, workspace).id if workspace is not None else None
        )
        repository = AppRepository(session)
        app_id = try_uuid(app_id_or_name)
        if app_id is not None:
            record = (
                repository.get(app_id, workspace_id=workspace_id)
                if workspace_id is not None
                else repository.get_across_workspaces(app_id)
            )
        elif workspace_id is not None:
            record = repository.get_by_name(app_id_or_name, workspace_id=workspace_id)
        else:
            record = repository.latest_by_name_across_workspaces(app_id_or_name)
        if record is not None:
            return record
        raise NotFoundError(f"app not found: {app_id_or_name}")


@dataclass(frozen=True, slots=True)
class DatabaseDeploymentReader:
    context: ControlContext

    def get(self, deployment_id_or_name: str) -> Deployment:
        with self.context.database.session() as session:
            repository = DeploymentRepository(session)
            record = repository.get_across_workspaces(deployment_id_or_name)
            if record is None:
                record = repository.latest_by_name(deployment_id_or_name, workspace_id=None)
        if record is None:
            raise NotFoundError(f"deployment not found: {deployment_id_or_name}")
        return record
