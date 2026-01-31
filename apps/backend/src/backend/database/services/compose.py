from datetime import UTC, datetime, timedelta

from models.deployments import DeploymentStates
from sqlalchemy import (
    func,
    or_,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import (
    ComposeDeploymentPydantic,
    WorkspaceRole,
)
from backend.database.services.base import DatabaseService
from backend.database.tables import (
    ComposeDeploymentTable,
    UserWorkspaceTable,
    WorkspaceTable,
)


class ComposeDeploymentService(
    DatabaseService[ComposeDeploymentTable, ComposeDeploymentPydantic]
):
    """Service layer for compose deployment operations"""

    def __init__(self, session: AsyncSession):
        super().__init__(ComposeDeploymentTable, ComposeDeploymentPydantic, session)

    async def get_by_name(
        self, workspace_id: str, name: str
    ) -> ComposeDeploymentPydantic | None:
        """Get deployment by name (excluding soft-deleted)."""
        query = (
            select(ComposeDeploymentTable)
            .where(ComposeDeploymentTable.workspace_id == workspace_id)
            .where(ComposeDeploymentTable.name == name)
            .where(ComposeDeploymentTable.deleted_at.is_(None))
        )
        result = await self._session.execute(query)
        db_model = result.scalar_one_or_none()
        return self._to_pydantic(db_model)

    async def find_by_namespace(
        self, workspace_id: str, namespace: str
    ) -> ComposeDeploymentPydantic | None:
        """Find deployment by namespace and workspace (excluding soft-deleted)."""
        query = (
            select(ComposeDeploymentTable)
            .where(ComposeDeploymentTable.workspace_id == workspace_id)
            .where(ComposeDeploymentTable.namespace == namespace)
            .where(ComposeDeploymentTable.deleted_at.is_(None))
        )
        result = await self._session.execute(query)
        db_model = result.scalar_one_or_none()
        return self._to_pydantic(db_model)

    async def find_by_status(
        self, workspace_id: str, state: DeploymentStates
    ) -> list[ComposeDeploymentPydantic]:
        """Find deployments by status (excluding soft-deleted)."""
        query = (
            select(ComposeDeploymentTable)
            .where(ComposeDeploymentTable.workspace_id == workspace_id)
            .where(ComposeDeploymentTable.state == state)
            .where(ComposeDeploymentTable.deleted_at.is_(None))
        )
        result = await self._session.execute(query)
        db_models = list(result.scalars().all())
        return [self._to_pydantic(db_model) for db_model in db_models]

    async def update_status(
        self,
        deployment_id: str,
        state: DeploymentStates,
        message: str | None = None,
    ) -> ComposeDeploymentPydantic | None:
        """Update deployment status."""
        deployment = await self.get_by_id(deployment_id)
        if not deployment:
            return None

        deployment.state = state
        if message:
            deployment.status_message = message

        return await self.update(deployment)

    async def get_active_deployments_for_workspace(
        self, workspace_id: str
    ) -> dict[str, str]:
        """Get mapping of deployment_name -> deployment_id for active deployments."""
        query = (
            select(ComposeDeploymentTable.name, ComposeDeploymentTable.id)
            .where(ComposeDeploymentTable.workspace_id == workspace_id)
            .where(ComposeDeploymentTable.deleted_at.is_(None))
        )
        result = await self._session.execute(query)
        return {row.name: str(row.id) for row in result.all() if row.name is not None}

    async def find_one_with_lock(
        self,
        workspace_id: str,
        name: str,
    ) -> ComposeDeploymentPydantic | None:
        """Find a deployment by workspace and name with row-level lock (excluding soft-deleted)"""
        query = (
            select(ComposeDeploymentTable)
            .where(ComposeDeploymentTable.workspace_id == workspace_id)
            .where(ComposeDeploymentTable.name == name)
            .where(ComposeDeploymentTable.deleted_at.is_(None))
            .with_for_update()
        )
        result = await self._session.execute(query)
        db_model = result.scalar_one_or_none()
        return self._to_pydantic(db_model)

    async def get_with_workspace_access(
        self, deployment_id: str, user_id: str, include_deleted: bool = False
    ) -> tuple[ComposeDeploymentPydantic | None, str | None]:
        """Get deployment and user's workspace role"""
        query = (
            select(ComposeDeploymentTable, UserWorkspaceTable.role)
            .join(
                UserWorkspaceTable,
                ComposeDeploymentTable.workspace_id == UserWorkspaceTable.workspace_id,
            )
            .where(ComposeDeploymentTable.id == deployment_id)
            .where(UserWorkspaceTable.user_id == user_id)
        )

        if not include_deleted:
            query = query.where(ComposeDeploymentTable.deleted_at.is_(None))

        result = await self._session.execute(query)
        row = result.first()

        if not row:
            return None, None

        deployment, role = row
        deployment_pydantic = self._to_pydantic(deployment)
        return deployment_pydantic, role

    async def find_active_during_date_range(
        self,
        workspace_id: str,
        start_date: datetime,
        end_date: datetime,
        limit: int = 100,
    ) -> list[ComposeDeploymentPydantic]:
        """Get deployments that existed during the date range."""
        query = (
            select(ComposeDeploymentTable)
            .where(ComposeDeploymentTable.workspace_id == workspace_id)
            .where(
                or_(
                    ComposeDeploymentTable.deleted_at.is_(None),
                    ComposeDeploymentTable.deleted_at >= start_date,
                )
            )
            .where(ComposeDeploymentTable.created_at <= end_date)
            .limit(limit)
        )
        result = await self._session.execute(query)
        return [self._to_pydantic(d) for d in result.scalars().all()]

    async def get_deployment_count(self, workspace_id: str) -> int:
        """Count active deployments for a workspace using a single COUNT query."""
        query = (
            select(func.count(ComposeDeploymentTable.id))
            .where(ComposeDeploymentTable.workspace_id == workspace_id)
            .where(ComposeDeploymentTable.deleted_at.is_(None))
        )
        result = await self._session.execute(query)
        return result.scalar() or 0

    async def get_deployment_counts_by_workspace(
        self, workspace_ids: list[str]
    ) -> dict[str, int]:
        """Get deployment counts for multiple workspaces in a single query."""
        if not workspace_ids:
            return {}

        query = (
            select(
                ComposeDeploymentTable.workspace_id,
                func.count(ComposeDeploymentTable.id).label("deployment_count"),
            )
            .where(ComposeDeploymentTable.workspace_id.in_(workspace_ids))
            .where(ComposeDeploymentTable.deleted_at.is_(None))
            .group_by(ComposeDeploymentTable.workspace_id)
        )
        result = await self._session.execute(query)
        return {str(row.workspace_id): row.deployment_count for row in result.all()}

    async def get_total_deployment_count_for_user(self, user_id: str) -> int:
        """Count total active deployments across all workspaces owned by a user.

        Deployment limits are enforced against the workspace OWNER's subscription,
        so this counts only deployments in workspaces where the user is the owner.
        Team members creating deployments count against the owner's limit.
        """
        query = (
            select(func.count(ComposeDeploymentTable.id))
            .join(
                UserWorkspaceTable,
                ComposeDeploymentTable.workspace_id == UserWorkspaceTable.workspace_id,
            )
            .where(UserWorkspaceTable.user_id == user_id)
            .where(UserWorkspaceTable.role == WorkspaceRole.OWNER)
            .where(ComposeDeploymentTable.deleted_at.is_(None))
        )
        result = await self._session.execute(query)
        return result.scalar() or 0

    async def find_stuck_deploying(
        self, minutes_old: int = 10
    ) -> list[ComposeDeploymentPydantic]:
        """Find deployments in DEPLOYING state older than specified minutes."""
        threshold = datetime.now(UTC) - timedelta(minutes=minutes_old)
        query = (
            select(ComposeDeploymentTable)
            .where(ComposeDeploymentTable.state == DeploymentStates.DEPLOYING)
            .where(ComposeDeploymentTable.updated_at < threshold)
            .where(ComposeDeploymentTable.deleted_at.is_(None))
        )
        result = await self._session.execute(query)
        db_models = list(result.scalars().all())
        return [self._to_pydantic(db_model) for db_model in db_models]

    async def find_orphaned_in_deleted_workspaces(
        self,
    ) -> list[ComposeDeploymentPydantic]:
        """Find deployments in deleted workspaces that haven't been cleaned up yet."""
        query = (
            select(ComposeDeploymentTable)
            .join(
                WorkspaceTable,
                ComposeDeploymentTable.workspace_id == WorkspaceTable.id,
            )
            .where(WorkspaceTable.deleted_at.isnot(None))
            .where(ComposeDeploymentTable.deleted_at.isnot(None))
            .where(ComposeDeploymentTable.state != DeploymentStates.DELETED.value)
            .where(ComposeDeploymentTable.current_task_run_id.is_(None))
        )
        result = await self._session.execute(query)
        db_models = list(result.scalars().all())
        return [self._to_pydantic(db_model) for db_model in db_models]

    async def find_pending_depot_cleanup(
        self,
    ) -> list[ComposeDeploymentPydantic]:
        """Find DELETED deployments with Depot projects that still need cleanup."""
        query = (
            select(ComposeDeploymentTable)
            .where(ComposeDeploymentTable.state == DeploymentStates.DELETED.value)
            .where(ComposeDeploymentTable.depot_project_id.isnot(None))
        )
        result = await self._session.execute(query)
        db_models = list(result.scalars().all())
        return [self._to_pydantic(db_model) for db_model in db_models]

    async def find_stale_pending(
        self, threshold: datetime
    ) -> list[ComposeDeploymentPydantic]:
        """Find PENDING deployments older than threshold.

        These are likely orphaned from failed builds or abandoned deploys.
        """
        query = (
            select(ComposeDeploymentTable)
            .where(ComposeDeploymentTable.state == DeploymentStates.PENDING)
            .where(ComposeDeploymentTable.created_at < threshold)
            .where(ComposeDeploymentTable.deleted_at.is_(None))
        )
        result = await self._session.execute(query)
        db_models = list(result.scalars().all())
        return [self._to_pydantic(db_model) for db_model in db_models]

    async def find_by_cluster(self, cluster_id: str) -> list[ComposeDeploymentPydantic]:
        """Find all active deployments on a specific cluster."""
        query = (
            select(ComposeDeploymentTable)
            .where(ComposeDeploymentTable.cluster_id == cluster_id)
            .where(ComposeDeploymentTable.deleted_at.is_(None))
        )
        result = await self._session.execute(query)
        return [self._to_pydantic(d) for d in result.scalars().all()]
