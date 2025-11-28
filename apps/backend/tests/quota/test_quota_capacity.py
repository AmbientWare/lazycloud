"""Tests for verify_quota_capacity function."""

from unittest.mock import AsyncMock

import pytest
from backend.database import Database
from backend.database.users import UserPydantic
from backend.database.workspaces import WorkspacePydantic
from backend.prefect_app.deployment.utils import verify_quota_capacity
from models.deployments import DeploymentStates
from models.helm import HelmValues, VolumeValues

from tests.fixtures.database import (
    make_deployment,
    make_features,
    make_service,
    requires_db,
)

pytestmark = [pytest.mark.asyncio, requires_db]


class TestVerifyQuotaCapacity:
    """Tests for verify_quota_capacity function."""

    async def test_deployment_limit_exceeded_raises_error(
        self,
        db_user_with_workspace: tuple[UserPydantic, WorkspacePydantic],
        db: Database,
        mock_subscription_service,
    ):
        """Raises ValueError when deployment limit exceeded."""
        _, workspace = db_user_with_workspace
        features = make_features(deployment_limit=2)
        mock_subscription_service.get_user_features = AsyncMock(return_value=features)

        for i in range(2):
            await db.compose_deployments.create(
                make_deployment(workspace.id, name=f"deploy-{i}")
            )

        with pytest.raises(ValueError, match="Deployment limit exceeded"):
            await verify_quota_capacity(
                workspace_id=workspace.id,
                required_deployments=1,
                required_services=1,
                required_pvcs=0,
            )

    async def test_service_limit_exceeded_raises_error(
        self,
        db_user_with_workspace: tuple[UserPydantic, WorkspacePydantic],
        db: Database,
        mock_subscription_service,
    ):
        """Raises ValueError when service limit exceeded."""
        _, workspace = db_user_with_workspace
        features = make_features(deployment_limit=5, service_limit=2)
        mock_subscription_service.get_user_features = AsyncMock(return_value=features)

        deployment = await db.compose_deployments.create(
            make_deployment(workspace.id, name="existing")
        )
        deployment.helm_values = HelmValues(
            services=[make_service(f"svc-{i}") for i in range(8)],
            volumes=[],
            networks=[],
            secrets=[],
        )
        await db.compose_deployments.update(deployment)

        with pytest.raises(ValueError, match="Service limit exceeded"):
            await verify_quota_capacity(
                workspace_id=workspace.id,
                required_deployments=1,
                required_services=5,
                required_pvcs=0,
            )

    async def test_pvc_limit_exceeded_raises_error(
        self,
        db_user_with_workspace: tuple[UserPydantic, WorkspacePydantic],
        db: Database,
        mock_subscription_service,
    ):
        """Raises ValueError when PVC/volume limit exceeded."""
        _, workspace = db_user_with_workspace
        features = make_features(deployment_limit=5, volume_limit=2)
        mock_subscription_service.get_user_features = AsyncMock(return_value=features)

        deployment = await db.compose_deployments.create(
            make_deployment(workspace.id, name="existing")
        )
        deployment.helm_values = HelmValues(
            services=[],
            volumes=[VolumeValues(name=f"vol-{i}", size="1Gi") for i in range(9)],
            networks=[],
            secrets=[],
        )
        await db.compose_deployments.update(deployment)

        with pytest.raises(ValueError, match="Volume limit exceeded"):
            await verify_quota_capacity(
                workspace_id=workspace.id,
                required_deployments=1,
                required_services=1,
                required_pvcs=3,
            )

    async def test_existing_resources_subtracted_for_updates(
        self,
        db_user_with_workspace: tuple[UserPydantic, WorkspacePydantic],
        db: Database,
        mock_subscription_service,
    ):
        """Existing resources are subtracted when updating deployment."""
        _, workspace = db_user_with_workspace
        features = make_features(deployment_limit=2, service_limit=5, volume_limit=3)
        mock_subscription_service.get_user_features = AsyncMock(return_value=features)

        for i in range(2):
            deployment = await db.compose_deployments.create(
                make_deployment(workspace.id, name=f"deploy-{i}")
            )
            deployment.helm_values = HelmValues(
                services=[make_service(f"svc-{i}-{j}") for j in range(3)],
                volumes=[VolumeValues(name=f"vol-{i}", size="1Gi")],
                networks=[],
                secrets=[],
            )
            await db.compose_deployments.update(deployment)

        with pytest.raises(ValueError, match="limit exceeded"):
            await verify_quota_capacity(
                workspace_id=workspace.id,
                required_deployments=1,
                required_services=3,
                required_pvcs=1,
            )

        # With existing_ params (simulating update), should succeed
        await verify_quota_capacity(
            workspace_id=workspace.id,
            required_deployments=1,
            required_services=3,
            required_pvcs=1,
            existing_deployments=1,
            existing_services=3,
            existing_pvcs=1,
        )

    async def test_deleted_deployments_not_counted(
        self,
        db_user_with_workspace: tuple[UserPydantic, WorkspacePydantic],
        db: Database,
        mock_subscription_service,
    ):
        """Deleted deployments are not counted toward limits."""
        _, workspace = db_user_with_workspace
        features = make_features(deployment_limit=2)
        mock_subscription_service.get_user_features = AsyncMock(return_value=features)

        await db.compose_deployments.create(
            make_deployment(
                workspace.id, name="deploy-active", state=DeploymentStates.DEPLOYED
            )
        )
        await db.compose_deployments.create(
            make_deployment(
                workspace.id, name="deploy-deleted", state=DeploymentStates.DELETED
            )
        )

        await verify_quota_capacity(
            workspace_id=workspace.id,
            required_deployments=1,
            required_services=1,
            required_pvcs=0,
        )
