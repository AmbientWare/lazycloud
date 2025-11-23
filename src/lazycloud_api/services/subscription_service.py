import json
from typing import TYPE_CHECKING, Any

import yaml
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from lazycloud_api.billing.product_details.base import BASE_FEATURES
from lazycloud_api.billing.product_details.features import BaseFeatures
from lazycloud_api.database import db
from lazycloud_api.database.users import SubscriptionState, UserPydantic
from lazycloud_api.database.workspaces import WorkspaceStatus
from lazycloud_api.services.compose.parser import ComposeParser
from lazycloud_api.services.polar import PolarService

if TYPE_CHECKING:
    from shared.models.compose import ComposeFile


class SubscriptionLimitError(Exception):
    """Exception raised when subscription limits are exceeded."""

    def __init__(self, message: str, status_code: int = 403):
        self.status_code = status_code
        super().__init__(message)


class SubscriptionService:
    """Service for managing subscription features and limits."""

    def __init__(self, polar_service: PolarService):
        self.polar_service = polar_service

    def _parse_features_from_metadata(
        self, metadata: dict[str, str] | None
    ) -> BaseFeatures | None:
        """Parse features from product metadata JSON string"""
        if not metadata:
            return None

        features_json = metadata.get("features")
        if not features_json:
            return None

        try:
            features_dict = json.loads(features_json)
            return BaseFeatures.model_validate(features_dict)

        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.warning(f"Failed to parse features from metadata: {e}")
            return None

    async def get_user_features(self, external_customer_id: str) -> BaseFeatures:
        """Get product features for a user based on their subscription"""
        if not self.polar_service.enabled:
            logger.debug("Polar disabled, returning Basic features")
            return BASE_FEATURES

        try:
            subscriptions_response = (
                await self.polar_service.client.subscriptions.list_async(
                    external_customer_id=external_customer_id,
                    active=True,
                    limit=1,
                )
            )
        except Exception as e:
            logger.error(
                f"Failed to fetch subscriptions from Polar for {external_customer_id}: {e}"
            )
            raise RuntimeError(
                f"Failed to retrieve subscription information: {e}"
            ) from e

        if (
            not subscriptions_response
            or not subscriptions_response.result
            or not subscriptions_response.result.items
        ):
            raise ValueError(
                f"No active subscription found for {external_customer_id}. "
                f"User must have an active subscription."
            )

        subscription = subscriptions_response.result.items[0]
        if not subscription.product:
            raise ValueError(
                f"Subscription {subscription.id} has no associated product. "
                f"This is a configuration error."
            )

        product = await self.polar_service.products.get_product(subscription.product.id)
        if not product:
            raise ValueError(
                f"Product {subscription.product.id} not found. "
                f"This is a configuration error."
            )

        # Parse features from metadata (required)
        features = self._parse_features_from_metadata(product.metadata)
        if features is None:
            raise ValueError(
                f"Product {subscription.product.id} has no features in metadata. "
                f"Please update the product metadata with features JSON."
            )

        logger.debug(
            f"Loaded features from product metadata for {external_customer_id}"
        )
        return features

    async def _update_user_subscription_state(
        self,
        user: UserPydantic,
        new_state: SubscriptionState,
        session: AsyncSession | None,
    ) -> UserPydantic:
        """Update user's subscription_state and return updated Pydantic model."""
        user.subscription_state = new_state
        updated = await db.users.update(user, session=session)
        if updated is None:
            logger.warning(
                f"aupdate returned None for user {user.id} - using in-memory state. "
                "This should not happen if user was just fetched."
            )
        return updated or user

    async def _audit_and_update_subscription_state(
        self, user_id: str, features: BaseFeatures, session: AsyncSession | None = None
    ) -> UserPydantic | None:
        """Audit user's resource usage and update subscription_state accordingly"""
        user = await db.users.get_by_id(user_id, session=session)
        if not user:
            return None

        workspace_count = await db.workspaces.get_active_workspace_count(user_id)

        if workspace_count > features.workspace.limit:
            if user.subscription_state != SubscriptionState.OVER_LIMITS:
                user = await self._update_user_subscription_state(
                    user, SubscriptionState.OVER_LIMITS, session
                )
                logger.warning(
                    f"User {user.email} (ID: {user_id}) exceeded workspace limit: "
                    f"{workspace_count}/{features.workspace.limit}. Setting OVER_LIMITS status."
                )
            return user

        if workspace_count > 0:
            workspaces_with_membership = (
                await db.workspaces.get_user_workspaces_with_membership(
                    user_id, status=WorkspaceStatus.ACTIVE
                )
            )
            workspace_ids = [ws.id for ws, _ in workspaces_with_membership]
            deployment_counts = (
                await db.compose_deployments.get_deployment_counts_by_workspace(
                    workspace_ids
                )
            )

            for workspace, _membership in workspaces_with_membership:
                deployment_count = deployment_counts.get(workspace.id, 0)
                if deployment_count > features.workspace.deployment_limit:
                    if user.subscription_state != SubscriptionState.OVER_LIMITS:
                        user = await self._update_user_subscription_state(
                            user, SubscriptionState.OVER_LIMITS, session
                        )
                        logger.warning(
                            f"User {user.email} (ID: {user_id}) exceeded deployment limit in workspace "
                            f"{workspace.id}. Setting OVER_LIMITS status."
                        )
                    return user

        if user.subscription_state == SubscriptionState.OVER_LIMITS:
            user = await self._update_user_subscription_state(
                user, SubscriptionState.WITHIN_LIMITS, session
            )
            logger.info(
                f"User {user.email} (ID: {user_id}) is now within limits. "
                "Resetting to WITHIN_LIMITS status."
            )

        return user

    async def check_workspace_limit(self, user_id: str, features: BaseFeatures) -> None:
        """Check if user can create a new workspace based on their subscription tier."""
        workspace_count = await db.workspaces.get_active_workspace_count(user_id)

        if workspace_count >= features.workspace.limit:
            raise SubscriptionLimitError(
                f"Workspace limit reached. Your plan allows {features.workspace.limit} workspace(s). "
                "Please upgrade your plan to create more workspaces.",
            )

    async def check_deployment_limit(
        self, workspace_id: str, features: BaseFeatures, user_id: str | None = None
    ) -> None:
        """Check if user can create a new deployment in the workspace based on their subscription tier."""
        deployment_count = await db.compose_deployments.get_deployment_count(
            workspace_id
        )

        if deployment_count >= features.workspace.deployment_limit:
            raise SubscriptionLimitError(
                f"Deployment limit reached for this workspace. Your plan allows {features.workspace.deployment_limit} deployment(s) per workspace. "
                "Please upgrade your plan to create more deployments.",
            )

    def extract_custom_domains_from_compose(
        self, compose_data: dict[str, Any]
    ) -> list[str]:
        """Extract custom domains from compose file service labels.

        Looks for domains in service labels like:
        - lazycloud.domain
        - lazycloud.ingress.domain
        - deploy.labels with domain information
        """
        custom_domains = set()
        services = compose_data.get("services", {})

        for _, service_config in services.items():
            if not isinstance(service_config, dict):
                continue

            # Check top-level labels
            labels = service_config.get("labels", {})
            if isinstance(labels, dict):
                domain = labels.get("lazycloud.domain") or labels.get(
                    "lazycloud.ingress.domain"
                )
                if domain:
                    custom_domains.add(domain)

            # Check deploy.labels
            deploy = service_config.get("deploy", {})
            if isinstance(deploy, dict):
                deploy_labels = deploy.get("labels", {})
                if isinstance(deploy_labels, dict):
                    domain = deploy_labels.get("lazycloud.domain") or deploy_labels.get(
                        "lazycloud.ingress.domain"
                    )
                    if domain:
                        custom_domains.add(domain)

        return list(custom_domains)

    async def check_deployment_features(
        self,
        compose_file: "ComposeFile",
        compose_data: dict[str, Any],
        features: BaseFeatures,
    ) -> None:
        """Check if deployment features (services, volumes, networks, domains) are within subscription limits."""

        # Extract custom domains from compose file service labels
        custom_domains = self.extract_custom_domains_from_compose(compose_data)

        # Count services
        service_count = len(compose_file.services)
        if service_count > features.deployment.service_limit:
            raise SubscriptionLimitError(
                f"Service limit exceeded. Your plan allows {features.deployment.service_limit} service(s) per deployment, "
                f"but this deployment has {service_count}. Please upgrade your plan or reduce the number of services.",
            )

        # Count volumes (unique volume names)
        volume_names = set()
        for service in compose_file.services:
            if service.volumes:
                for volume in service.volumes:
                    if volume.source:
                        volume_names.add(volume.source)
        volume_count = len(volume_names) + len(compose_file.volumes)
        if volume_count > features.deployment.volume_limit:
            raise SubscriptionLimitError(
                f"Volume limit exceeded. Your plan allows {features.deployment.volume_limit} volume(s) per deployment, "
                f"but this deployment has {volume_count}. Please upgrade your plan or reduce the number of volumes.",
            )

        # Count networks (unique network names)
        network_names = set()
        for service in compose_file.services:
            if service.networks:
                for network in service.networks:
                    network_names.add(network.name)
        network_count = len(network_names) + len(compose_file.networks)
        if network_count > features.deployment.network_limit:
            raise SubscriptionLimitError(
                f"Network limit exceeded. Your plan allows {features.deployment.network_limit} network(s) per deployment, "
                f"but this deployment has {network_count}. Please upgrade your plan or reduce the number of networks.",
            )

        # Check replicas per service
        max_replicas = features.deployment.max_replicas_per_service
        for service in compose_file.services:
            replicas = None
            # Check deploy.replicas first
            if service.deploy and service.deploy.replicas is not None:
                replicas = service.deploy.replicas
            # Check scaling.min if scaling is enabled
            elif service.scaling and service.scaling.enabled:
                replicas = service.scaling.min

            if replicas is not None and replicas > max_replicas:
                raise SubscriptionLimitError(
                    f"Replica limit exceeded for service '{service.name}'. Your plan allows {max_replicas} replica(s) per service, "
                    f"but this service has {replicas}. Please upgrade your plan or reduce the number of replicas.",
                )

            # Check HPA max replicas if scaling is enabled
            if (
                service.scaling
                and service.scaling.enabled
                and service.scaling.max > max_replicas
            ):
                raise SubscriptionLimitError(
                    f"HPA max replica limit exceeded for service '{service.name}'. Your plan allows {max_replicas} replica(s) per service, "
                    f"but HPA max is set to {service.scaling.max}. Please upgrade your plan or reduce the max replicas.",
                )

        # Check custom domains
        if features.domain_limit == 0 and len(custom_domains) > 0:
            raise SubscriptionLimitError(
                "Custom domains are not available on your plan. Please upgrade to a plan that supports custom domains.",
            )

    async def validate_workspace_for_owner(
        self, workspace_id: str, new_owner_features: BaseFeatures, new_owner_id: str
    ) -> None:
        """Validate that a workspace can be transferred to a new owner based on their plan features."""
        # Check workspace limit: new owner must have room for one more workspace
        workspace_count = await db.workspaces.get_active_workspace_count(new_owner_id)
        if workspace_count >= new_owner_features.workspace.limit:
            raise SubscriptionLimitError(
                f"Workspace limit reached. Your plan allows {new_owner_features.workspace.limit} workspace(s), "
                f"and you currently own {workspace_count}. Please upgrade your plan to accept this workspace transfer.",
            )

        # Check deployment limit: workspace's deployment count must not exceed new owner's limit
        deployment_count = await db.compose_deployments.get_deployment_count(
            workspace_id
        )
        if deployment_count > new_owner_features.workspace.deployment_limit:
            raise SubscriptionLimitError(
                f"Deployment limit exceeded. This workspace has {deployment_count} deployment(s), "
                f"but your plan allows {new_owner_features.workspace.deployment_limit} deployment(s) per workspace. "
                "Please upgrade your plan to accept this workspace transfer.",
            )

        # Validate each deployment's features against new owner's plan
        _, deployments = await db.compose_deployments.find_paginated(
            filters={"workspace_id": workspace_id},
            offset=0,
            limit=1000,  # Get all deployments
            include_deleted=False,
        )

        validation_errors = []
        for deployment in deployments:
            if not deployment.compose_yaml:
                continue

            try:
                compose_data = yaml.safe_load(deployment.compose_yaml)
                if not compose_data:
                    continue

                compose_file = ComposeParser.parse_dict(compose_data)
                # This will raise SubscriptionLimitError if validation fails
                await self.check_deployment_features(
                    compose_file, compose_data, new_owner_features
                )

            except SubscriptionLimitError as e:
                deployment_name = deployment.name or deployment.id
                validation_errors.append(f"Deployment '{deployment_name}': {str(e)}")

            except Exception as e:
                logger.error(
                    f"Error validating deployment {deployment.id} for workspace transfer: {e}"
                )
                deployment_name = deployment.name or deployment.id
                validation_errors.append(
                    f"Deployment '{deployment_name}': Failed to validate - {str(e)}"
                )

        if validation_errors:
            error_message = (
                "The following deployment(s) exceed your plan limits:\n"
                + "\n".join(f"  - {error}" for error in validation_errors)
                + "\n\nPlease upgrade your plan to accept this workspace transfer."
            )
            raise SubscriptionLimitError(error_message)
