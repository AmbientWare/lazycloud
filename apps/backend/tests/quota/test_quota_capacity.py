"""Tests for verify_quota_capacity function."""

from unittest.mock import AsyncMock

import pytest
from backend.database import Database
from backend.database.models import WorkspaceRole
from backend.tasks.core.utils import verify_quota_capacity

from tests.fixtures.database import (
    make_deployment,
    make_features,
    make_user,
    make_user_workspace,
    make_workspace,
    requires_db,
)

pytestmark = [pytest.mark.asyncio, requires_db]


class TestVerifyQuotaCapacity:
    """Tests for verify_quota_capacity function.

    Note: Only deployment limit is checked. Services, volumes, and networks
    are unlimited (usage billing handles cost).
    """

    async def test_deployment_limit_exceeded_raises_error(
        self,
        db: Database,
        mock_subscription_service,
    ):
        """Raises ValueError when total deployment limit exceeded across all workspaces."""
        # Create user with 2 workspaces
        user = await db.users.create(make_user())
        workspace1 = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(
            make_user_workspace(user.id, workspace1.id, WorkspaceRole.OWNER)
        )
        workspace2 = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(
            make_user_workspace(user.id, workspace2.id, WorkspaceRole.OWNER)
        )

        # Set deployment limit to 3
        features = make_features(deployment_limit=3)
        mock_subscription_service.get_user_features = AsyncMock(return_value=features)

        # Create 2 deployments in workspace1 and 1 in workspace2 (total = 3)
        await db.compose_deployments.create(
            make_deployment(workspace1.id, name="deploy-1")
        )
        await db.compose_deployments.create(
            make_deployment(workspace1.id, name="deploy-2")
        )
        await db.compose_deployments.create(
            make_deployment(workspace2.id, name="deploy-3")
        )

        # Should fail because total is 3, which equals the limit
        with pytest.raises(ValueError, match="Deployment limit exceeded"):
            await verify_quota_capacity(workspace1.id)

    async def test_within_limit_succeeds(
        self,
        db: Database,
        mock_subscription_service,
    ):
        """Succeeds when within deployment limit."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(
            make_user_workspace(user.id, workspace.id, WorkspaceRole.OWNER)
        )

        features = make_features(deployment_limit=5)
        mock_subscription_service.get_user_features = AsyncMock(return_value=features)

        await db.compose_deployments.create(
            make_deployment(workspace.id, name="deploy-1")
        )

        # Should succeed - only 1 deployment, limit is 5
        await verify_quota_capacity(workspace.id)

    async def test_update_skips_limit_check(
        self,
        db: Database,
        mock_subscription_service,
    ):
        """Updates skip the limit check since they don't create new deployments."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(
            make_user_workspace(user.id, workspace.id, WorkspaceRole.OWNER)
        )

        features = make_features(deployment_limit=2)
        mock_subscription_service.get_user_features = AsyncMock(return_value=features)

        await db.compose_deployments.create(
            make_deployment(workspace.id, name="deploy-1")
        )
        await db.compose_deployments.create(
            make_deployment(workspace.id, name="deploy-2")
        )

        # Without is_update, should fail (2 deployments = limit)
        with pytest.raises(ValueError, match="Deployment limit exceeded"):
            await verify_quota_capacity(workspace.id)

        # With is_update=True, should succeed (updating existing, not creating new)
        await verify_quota_capacity(workspace.id, is_update=True)

    async def test_no_owner_skips_check(
        self,
        db: Database,
        mock_subscription_service,
    ):
        """Workspace without owner skips quota check (returns without error)."""
        # Create workspace without owner
        workspace = await db.workspaces.create(make_workspace())

        # Should not raise - just logs warning and returns
        await verify_quota_capacity(workspace.id)
