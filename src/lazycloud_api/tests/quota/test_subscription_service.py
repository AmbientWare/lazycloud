"""Tests for SubscriptionService limit enforcement."""

import pytest
import yaml

from lazycloud_api.database import Database
from lazycloud_api.database.users import UserPydantic
from lazycloud_api.database.workspaces import WorkspacePydantic
from lazycloud_api.services.compose.parser import ComposeParser
from lazycloud_api.services.subscription_service import (
    SubscriptionLimitError,
    SubscriptionService,
)
from lazycloud_api.tests.fixtures.database import (
    make_deployment,
    make_features,
    make_user,
    make_user_workspace,
    make_workspace,
    requires_db,
)
from shared.models.workspaces import WorkspaceRole

pytestmark = [pytest.mark.asyncio, requires_db]


class TestCheckWorkspaceLimit:
    """Tests for check_workspace_limit."""

    async def test_user_at_limit_cannot_create_workspace(
        self, subscription_service: SubscriptionService, db: Database
    ):
        """User at workspace limit cannot create new workspace."""
        user = await db.users.create(make_user())
        features = make_features(workspace_limit=2)

        for _ in range(2):
            workspace = await db.workspaces.create(make_workspace())
            await db.user_workspaces.create(
                make_user_workspace(user.id, workspace.id, WorkspaceRole.OWNER)
            )

        with pytest.raises(SubscriptionLimitError, match="Workspace limit reached"):
            await subscription_service.check_workspace_limit(user.id, features)


class TestCheckDeploymentLimit:
    """Tests for check_deployment_limit."""

    async def test_workspace_at_limit_cannot_create_deployment(
        self,
        subscription_service: SubscriptionService,
        db_user_with_workspace: tuple[UserPydantic, WorkspacePydantic],
        db: Database,
    ):
        """Workspace at deployment limit cannot create new deployment."""
        user, workspace = db_user_with_workspace
        features = make_features(deployment_limit=2)

        for i in range(2):
            await db.compose_deployments.create(
                make_deployment(workspace.id, name=f"deploy-{i}")
            )

        with pytest.raises(SubscriptionLimitError, match="Deployment limit reached"):
            await subscription_service.check_deployment_limit(
                workspace.id, features, user.id
            )


class TestCheckDeploymentFeatures:
    """Tests for check_deployment_features."""

    async def test_deployment_with_too_many_services_fails(
        self, subscription_service: SubscriptionService
    ):
        """Deployment with too many services fails validation."""
        services = {f"service-{i}": {"image": "nginx"} for i in range(11)}
        compose_data = {"version": "3.8", "services": services}
        compose_file = ComposeParser.parse_dict(compose_data)
        features = make_features(service_limit=10)

        with pytest.raises(SubscriptionLimitError, match="Service limit exceeded"):
            await subscription_service.check_deployment_features(compose_file, features)

    async def test_deployment_with_too_many_volumes_fails(
        self, subscription_service: SubscriptionService
    ):
        """Deployment with too many volumes fails validation."""
        volumes = {f"vol-{i}": {} for i in range(6)}
        volume_mounts = [f"vol-{i}:/data{i}" for i in range(6)]
        compose_data = {
            "version": "3.8",
            "services": {"web": {"image": "nginx", "volumes": volume_mounts}},
            "volumes": volumes,
        }
        compose_file = ComposeParser.parse_dict(compose_data)
        features = make_features(volume_limit=5)

        with pytest.raises(SubscriptionLimitError, match="Volume limit exceeded"):
            await subscription_service.check_deployment_features(compose_file, features)

    async def test_deployment_with_too_many_networks_fails(
        self, subscription_service: SubscriptionService
    ):
        """Deployment with too many networks fails validation."""
        network_names = [f"net-{i}" for i in range(4)]
        compose_data = {
            "version": "3.8",
            "services": {"web": {"image": "nginx", "networks": network_names}},
            "networks": network_names,
        }
        compose_file = ComposeParser.parse_dict(compose_data)
        features = make_features(network_limit=3)

        with pytest.raises(SubscriptionLimitError, match="Network limit exceeded"):
            await subscription_service.check_deployment_features(compose_file, features)

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

        with pytest.raises(SubscriptionLimitError, match="HPA max replica limit"):
            await subscription_service.check_deployment_features(compose_file, features)

    async def test_deployment_with_custom_domains_on_no_domain_plan_fails(
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
        features = make_features(domain_limit=0)

        with pytest.raises(SubscriptionLimitError, match="Custom domains are not"):
            await subscription_service.check_deployment_features(compose_file, features)


class TestValidateWorkspaceForOwner:
    """Tests for validate_workspace_for_owner."""

    async def test_transfer_fails_when_new_owner_at_workspace_limit(
        self,
        subscription_service: SubscriptionService,
        db_user_with_workspace: tuple[UserPydantic, WorkspacePydantic],
        db: Database,
    ):
        """Workspace transfer fails when new owner is at workspace limit."""
        _, workspace = db_user_with_workspace
        new_owner = await db.users.create(make_user())

        for _ in range(2):
            other_workspace = await db.workspaces.create(make_workspace())
            await db.user_workspaces.create(
                make_user_workspace(
                    new_owner.id, other_workspace.id, WorkspaceRole.OWNER
                )
            )

        new_owner_features = make_features(workspace_limit=2)

        with pytest.raises(SubscriptionLimitError, match="Workspace limit reached"):
            await subscription_service.validate_workspace_for_owner(
                workspace.id, new_owner_features, new_owner.id
            )

    async def test_transfer_fails_when_workspace_exceeds_deployment_limit(
        self,
        subscription_service: SubscriptionService,
        db_user_with_workspace: tuple[UserPydantic, WorkspacePydantic],
        db: Database,
    ):
        """Workspace transfer fails when workspace has more deployments than new owner's limit."""
        _, workspace = db_user_with_workspace
        new_owner = await db.users.create(make_user())

        for i in range(3):
            await db.compose_deployments.create(
                make_deployment(workspace.id, name=f"deploy-{i}")
            )

        new_owner_features = make_features(deployment_limit=2)

        with pytest.raises(SubscriptionLimitError, match="Deployment limit exceeded"):
            await subscription_service.validate_workspace_for_owner(
                workspace.id, new_owner_features, new_owner.id
            )

    async def test_transfer_fails_when_deployment_exceeds_feature_limits(
        self,
        subscription_service: SubscriptionService,
        db_user_with_workspace: tuple[UserPydantic, WorkspacePydantic],
        db: Database,
    ):
        """Workspace transfer fails when deployment exceeds new owner's feature limits."""
        _, workspace = db_user_with_workspace
        new_owner = await db.users.create(make_user())

        services = {f"service-{i}": {"image": "nginx"} for i in range(11)}
        deployment = await db.compose_deployments.create(
            make_deployment(workspace.id, name="test-deploy")
        )
        deployment.compose_yaml = yaml.dump({"version": "3.8", "services": services})
        await db.compose_deployments.update(deployment)

        new_owner_features = make_features(service_limit=10)

        with pytest.raises(SubscriptionLimitError, match="exceed your plan limits"):
            await subscription_service.validate_workspace_for_owner(
                workspace.id, new_owner_features, new_owner.id
            )
