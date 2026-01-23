"""Tests for SubscriptionService limit enforcement."""

import pytest
import yaml
from backend.database import Database
from backend.database.users import UserPydantic
from backend.database.workspaces import WorkspacePydantic
from backend.services.compose.parser import ComposeParser
from backend.services.subscription_service import (
    SubscriptionLimitError,
    SubscriptionService,
)
from models.workspaces import WorkspaceRole

from tests.fixtures.database import (
    make_deployment,
    make_features,
    make_user,
    make_user_workspace,
    make_workspace,
    requires_db,
)

pytestmark = [pytest.mark.asyncio, requires_db]


class TestCheckDeploymentLimit:
    """Tests for check_deployment_limit.

    Note: Deployment limit is now checked across ALL workspaces (total),
    not per-workspace.
    """

    async def test_user_at_total_deployment_limit_cannot_create_deployment(
        self,
        subscription_service: SubscriptionService,
        db: Database,
    ):
        """User at total deployment limit cannot create new deployment."""
        user = await db.users.create(make_user())
        features = make_features(deployment_limit=3)

        # Create 2 workspaces with deployments totaling the limit
        workspace1 = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(
            make_user_workspace(user.id, workspace1.id, WorkspaceRole.OWNER)
        )
        await db.compose_deployments.create(
            make_deployment(workspace1.id, name="deploy-1")
        )
        await db.compose_deployments.create(
            make_deployment(workspace1.id, name="deploy-2")
        )

        workspace2 = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(
            make_user_workspace(user.id, workspace2.id, WorkspaceRole.OWNER)
        )
        await db.compose_deployments.create(
            make_deployment(workspace2.id, name="deploy-3")
        )

        # Should fail because total is 3, which equals the limit
        with pytest.raises(SubscriptionLimitError, match="Deployment limit reached"):
            await subscription_service.check_deployment_limit(user.id, features)

    async def test_user_under_limit_can_create_deployment(
        self,
        subscription_service: SubscriptionService,
        db: Database,
    ):
        """User under deployment limit can create new deployment."""
        user = await db.users.create(make_user())
        features = make_features(deployment_limit=5)

        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(
            make_user_workspace(user.id, workspace.id, WorkspaceRole.OWNER)
        )
        await db.compose_deployments.create(
            make_deployment(workspace.id, name="deploy-1")
        )

        # Should pass - only 1 deployment, limit is 5
        await subscription_service.check_deployment_limit(user.id, features)


class TestCheckDeploymentFeatures:
    """Tests for check_deployment_features.

    Note: Services, volumes, and networks per deployment are NO LONGER limited.
    Only replicas, CPU, memory, and custom domains are checked.
    """

    async def test_service_with_too_many_replicas_fails(
        self, subscription_service: SubscriptionService
    ):
        """Service with too many replicas fails validation."""
        compose_data = {
            "version": "3.8",
            "services": {"web": {"image": "nginx", "deploy": {"replicas": 11}}},
        }
        compose_file = ComposeParser.parse_dict(compose_data)
        features = make_features(max_replicas=10)

        with pytest.raises(SubscriptionLimitError, match="Replica limit exceeded"):
            await subscription_service.check_deployment_features(compose_file, features)

    async def test_service_with_hpa_max_over_limit_fails(
        self, subscription_service: SubscriptionService
    ):
        """Service with HPA max replicas over limit fails validation."""
        compose_data = {
            "version": "3.8",
            "services": {
                "web": {
                    "image": "nginx",
                    "deploy": {
                        "labels": {
                            "lazycloud.scaling.enabled": "true",
                            "lazycloud.scaling.min": "1",
                            "lazycloud.scaling.max": "11",
                            "lazycloud.scaling.cpu": "80",
                            "lazycloud.scaling.memory": "80",
                        }
                    },
                }
            },
        }
        compose_file = ComposeParser.parse_dict(compose_data)
        features = make_features(max_replicas=10)

        with pytest.raises(SubscriptionLimitError, match="Auto-scaling limit"):
            await subscription_service.check_deployment_features(compose_file, features)

    async def test_service_with_too_much_cpu_fails(
        self, subscription_service: SubscriptionService
    ):
        """Service requesting too much CPU fails validation."""
        compose_data = {
            "version": "3.8",
            "services": {
                "web": {
                    "image": "nginx",
                    "deploy": {"resources": {"limits": {"cpus": "16"}}},
                }
            },
        }
        compose_file = ComposeParser.parse_dict(compose_data)
        features = make_features(max_cpu_per_service=8.0)

        with pytest.raises(SubscriptionLimitError, match="CPU limit exceeded"):
            await subscription_service.check_deployment_features(compose_file, features)

    async def test_service_with_too_much_memory_fails(
        self, subscription_service: SubscriptionService
    ):
        """Service requesting too much memory fails validation."""
        compose_data = {
            "version": "3.8",
            "services": {
                "web": {
                    "image": "nginx",
                    "deploy": {"resources": {"limits": {"memory": "32G"}}},
                }
            },
        }
        compose_file = ComposeParser.parse_dict(compose_data)
        features = make_features(max_memory_per_service=16)

        with pytest.raises(SubscriptionLimitError, match="Memory limit exceeded"):
            await subscription_service.check_deployment_features(compose_file, features)

    async def test_deployment_with_custom_domains_when_disabled_fails(
        self, subscription_service: SubscriptionService
    ):
        """Deployment with custom domains on plan without domain support fails."""
        compose_data = {
            "version": "3.8",
            "services": {
                "web": {
                    "image": "nginx",
                    "labels": {"lazycloud.domain": "example.com"},
                }
            },
        }
        compose_file = ComposeParser.parse_dict(compose_data)
        features = make_features(custom_domains_enabled=False)

        with pytest.raises(SubscriptionLimitError, match="Custom domains are not"):
            await subscription_service.check_deployment_features(compose_file, features)

    async def test_deployment_with_custom_domains_when_enabled_passes(
        self, subscription_service: SubscriptionService
    ):
        """Deployment with custom domains passes when enabled."""
        compose_data = {
            "version": "3.8",
            "services": {
                "web": {
                    "image": "nginx",
                    "labels": {"lazycloud.domain": "example.com"},
                }
            },
        }
        compose_file = ComposeParser.parse_dict(compose_data)
        features = make_features(custom_domains_enabled=True)

        # Should not raise
        await subscription_service.check_deployment_features(compose_file, features)

    async def test_deployment_with_many_services_passes(
        self, subscription_service: SubscriptionService
    ):
        """Deployment with many services passes (no service limit)."""
        services = {f"service-{i}": {"image": "nginx"} for i in range(20)}
        compose_data = {"version": "3.8", "services": services}
        compose_file = ComposeParser.parse_dict(compose_data)
        features = make_features()

        # Should not raise - services are not limited
        await subscription_service.check_deployment_features(compose_file, features)


class TestValidateWorkspaceForOwner:
    """Tests for validate_workspace_for_owner."""

    async def test_transfer_fails_when_would_exceed_deployment_limit(
        self,
        subscription_service: SubscriptionService,
        db: Database,
    ):
        """Workspace transfer fails when it would exceed new owner's total deployment limit."""
        # Create original owner with workspace and deployments
        original_owner = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(
            make_user_workspace(original_owner.id, workspace.id, WorkspaceRole.OWNER)
        )
        for i in range(3):
            await db.compose_deployments.create(
                make_deployment(workspace.id, name=f"deploy-{i}")
            )

        # Create new owner who already has some deployments
        new_owner = await db.users.create(make_user())
        new_owner_workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(
            make_user_workspace(
                new_owner.id, new_owner_workspace.id, WorkspaceRole.OWNER
            )
        )
        await db.compose_deployments.create(
            make_deployment(new_owner_workspace.id, name="existing-deploy")
        )

        # New owner has limit of 3, already has 1, workspace has 3 -> would be 4
        new_owner_features = make_features(deployment_limit=3)

        with pytest.raises(
            SubscriptionLimitError, match="Deployment limit would be exceeded"
        ):
            await subscription_service.validate_workspace_for_owner(
                workspace.id, new_owner_features, new_owner.id
            )

    async def test_transfer_fails_when_deployment_exceeds_cpu_limits(
        self,
        subscription_service: SubscriptionService,
        db: Database,
    ):
        """Workspace transfer fails when deployment exceeds new owner's CPU limits."""
        original_owner = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(
            make_user_workspace(original_owner.id, workspace.id, WorkspaceRole.OWNER)
        )

        # Create deployment with high CPU
        deployment = await db.compose_deployments.create(
            make_deployment(workspace.id, name="test-deploy")
        )
        deployment.compose_yaml = yaml.dump(
            {
                "version": "3.8",
                "services": {
                    "web": {
                        "image": "nginx",
                        "deploy": {"resources": {"limits": {"cpus": "16"}}},
                    }
                },
            }
        )
        await db.compose_deployments.update(deployment)

        new_owner = await db.users.create(make_user())
        new_owner_features = make_features(
            deployment_limit=10,
            max_cpu_per_service=8.0,
        )

        with pytest.raises(SubscriptionLimitError, match="exceed your plan limits"):
            await subscription_service.validate_workspace_for_owner(
                workspace.id, new_owner_features, new_owner.id
            )

    async def test_transfer_succeeds_when_within_limits(
        self,
        subscription_service: SubscriptionService,
        db: Database,
    ):
        """Workspace transfer succeeds when within new owner's limits."""
        original_owner = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(
            make_user_workspace(original_owner.id, workspace.id, WorkspaceRole.OWNER)
        )
        await db.compose_deployments.create(
            make_deployment(workspace.id, name="deploy-1")
        )

        new_owner = await db.users.create(make_user())
        new_owner_features = make_features(deployment_limit=10)

        # Should not raise
        await subscription_service.validate_workspace_for_owner(
            workspace.id, new_owner_features, new_owner.id
        )
