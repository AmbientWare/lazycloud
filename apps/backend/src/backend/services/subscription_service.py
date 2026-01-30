import json

import yaml
from loguru import logger
from models.compose import ComposeFile
from models.workspaces import InvitationType

from backend.billing.product_details.features import BaseFeatures
from backend.config import app_config
from backend.database import get_db_context
from backend.database.users import SubscriptionState, UserPydantic
from backend.services.cache import CacheService
from backend.services.compose.parser import ComposeParser
from backend.services.k8s.generators.converters import (
    parse_cpu_to_cores,
    parse_memory_to_gb,
)
from backend.services.polar import PolarService

# Cache key prefix and TTL for user features (5 minutes)
FEATURES_CACHE_PREFIX = "features:"
FEATURES_CACHE_TTL_SECONDS = 300


class SubscriptionLimitError(Exception):
    """Exception raised when subscription limits are exceeded."""

    def __init__(self, message: str, status_code: int = 403):
        self.status_code = status_code
        super().__init__(message)


class NoActiveSubscriptionError(Exception):
    """Exception raised when a user has no active subscription.

    This is a recoverable error - the user may be an admin/test user
    or simply needs to subscribe.
    """

    def __init__(self, external_customer_id: str):
        self.external_customer_id = external_customer_id
        super().__init__(
            f"No active subscription found for {external_customer_id}. "
            f"User must have an active subscription."
        )


class BillingNotConfiguredError(Exception):
    """Exception raised when billing service is not configured.

    This allows callers to handle the case where billing is disabled
    (e.g., local development) and potentially fall back to default features.
    """

    pass


class SubscriptionService:
    """Service for managing subscription features and limits."""

    def __init__(self, polar_service: PolarService):
        self.polar_service = polar_service
        self._cache = self._init_cache()

    def _init_cache(self) -> CacheService | None:
        """
        Initialize the cache.
        Only cache features if the webhook secret is configured.
        This is mainly a prod/dev feature toggle.
        We do not use the webhook in dev.
        """
        should_cache_features = bool(app_config.POLAR_WEBHOOK_SECRET)
        message = (
            "Subscription features will not be cached"
            if not should_cache_features
            else "Subscription features will be cached"
        )
        logger.info(message)
        return CacheService(redis_url=app_config.REDIS_URL) if should_cache_features else None

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
        """Get product features for a user based on their subscription.

        Args:
            external_customer_id: The external customer ID (e.g., workos_id)

        Returns:
            BaseFeatures for the user's subscription tier

        Raises:
            BillingNotConfiguredError: If billing service is not configured
            NoActiveSubscriptionError: If user has no active subscription (not cached)
            ValueError: If subscription or product configuration is invalid
            RuntimeError: If API call fails
        """

        if not self.polar_service.enabled:
            raise BillingNotConfiguredError(
                "Billing service is not configured. "
                "Callers should catch this exception and fall back to default features if appropriate."
            )

        # Check Redis cache first
        if self._cache is not None:
            cache_key = f"{FEATURES_CACHE_PREFIX}{external_customer_id}"
            cached_data = await self._cache.get(cache_key)
            if cached_data is not None:
                logger.debug(f"Using cached features for {external_customer_id}")
                return BaseFeatures.model_validate(cached_data)

        # Fetch from Polar API
        features = await self._fetch_user_features_from_polar(external_customer_id)

        # Cache successful lookups only (not NoActiveSubscriptionError)
        if self._cache is not None:
            await self._cache.set(
                cache_key,
                features.model_dump(),
                FEATURES_CACHE_TTL_SECONDS,
            )
            logger.debug(f"Cached features for {external_customer_id}")

        return features

    async def _fetch_user_features_from_polar(
        self, external_customer_id: str
    ) -> BaseFeatures:
        """Fetch user features from Polar API (no caching)."""
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
            raise NoActiveSubscriptionError(external_customer_id)

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

    async def clear_features_cache(
        self, external_customer_id: str | None = None
    ) -> None:
        """Clear features cache for a specific customer or all customers.

        Called by webhook handler when subscription changes.
        """

        if self._cache is None:
            logger.warning(
                "Subscription features are not cached - skipping cache clear"
            )
            return

        if external_customer_id:
            cache_key = f"{FEATURES_CACHE_PREFIX}{external_customer_id}"
            deleted = await self._cache.delete(cache_key)
            if deleted:
                logger.info(f"Cleared features cache for {external_customer_id}")
        else:
            # For clearing all, we'd need Redis SCAN/KEYS which isn't in CacheService
            # This is rarely used - just log a warning
            logger.warning(
                "clear_features_cache called without customer_id - "
                "Redis doesn't support clearing by prefix without SCAN"
            )

    async def _update_user_subscription_state(
        self,
        user: UserPydantic,
        new_state: SubscriptionState,
    ) -> UserPydantic:
        """Update user's subscription_state and return updated Pydantic model."""
        user.subscription_state = new_state
        async with get_db_context() as db:
            updated = await db.users.update(user)

        if updated is None:
            logger.warning(
                f"aupdate returned None for user {user.id} - using in-memory state. "
                "This should not happen if user was just fetched."
            )
        return updated or user

    async def _audit_and_update_subscription_state(
        self, user_id: str, features: BaseFeatures
    ) -> UserPydantic | None:
        """Audit user's resource usage and update subscription_state accordingly.

        Checks total deployments across all workspaces against the deployment limit.

        Note: This only manages OVER_LIMITS ↔ WITHIN_LIMITS transitions.
        Payment states (PAYMENT_FAILED, SUSPENDED) are managed by Polar webhooks
        and take precedence - this function won't overwrite them.
        """
        # Payment states managed by webhooks - don't overwrite
        PAYMENT_STATES = {
            SubscriptionState.PAYMENT_FAILED,
            SubscriptionState.SUSPENDED,
            SubscriptionState.TRIAL_EXPIRED,
        }

        async with get_db_context() as db:
            user = await db.users.get_by_id(user_id)

        if user is None:
            return None

        # Don't modify users with payment issues - those are managed by webhooks
        if user.subscription_state in PAYMENT_STATES:
            logger.debug(
                f"Skipping audit for user {user.email} - state {user.subscription_state.value} "
                "is managed by webhooks"
            )
            return user

        # Get total deployment count across all user's workspaces
        async with get_db_context() as db:
            total_deployments = (
                await db.compose_deployments.get_total_deployment_count_for_user(
                    user_id
                )
            )

        # Check if user exceeds total deployment limit
        if total_deployments > features.deployment_limit:
            if user.subscription_state != SubscriptionState.OVER_LIMITS:
                user = await self._update_user_subscription_state(
                    user, SubscriptionState.OVER_LIMITS
                )
                logger.warning(
                    f"User {user.email} (ID: {user_id}) exceeded deployment limit: "
                    f"{total_deployments}/{features.deployment_limit}. Setting OVER_LIMITS status."
                )
            return user

        # User is within limits
        if user.subscription_state == SubscriptionState.OVER_LIMITS:
            user = await self._update_user_subscription_state(
                user, SubscriptionState.WITHIN_LIMITS
            )
            logger.info(
                f"User {user.email} (ID: {user_id}) is now within limits. "
                "Resetting to WITHIN_LIMITS status."
            )

        return user

    async def check_deployment_limit(
        self, user_id: str, features: BaseFeatures
    ) -> None:
        """Check if user can create a new deployment based on their subscription tier.

        Checks total deployments across ALL workspaces against the deployment limit.
        """
        async with get_db_context() as db:
            total_deployments = (
                await db.compose_deployments.get_total_deployment_count_for_user(
                    user_id
                )
            )

        if total_deployments >= features.deployment_limit:
            raise SubscriptionLimitError(
                f"Deployment limit reached. Your plan allows {features.deployment_limit} deployment(s). "
                f"You currently have {total_deployments}. "
                "Please upgrade your plan to create more deployments.",
            )

    async def check_team_member_limit(
        self,
        workspace_id: str,
        email: str,
        features: BaseFeatures,
    ) -> None:
        """Check if inviting a new member would exceed the team member limit.

        Args:
            workspace_id: The workspace to check
            email: Email of the user being invited (to detect resends)
            features: Subscription features with max_team_members limit
        """

        if features.max_team_members is None:
            return

        async with get_db_context() as db:
            # Check if this email already has a pending invitation (resend case)
            existing_invitation = (
                await db.invitations.get_by_workspace_and_email_and_type(
                    workspace_id, email.lower().strip(), InvitationType.MEMBER.value
                )
            )
            if existing_invitation and not existing_invitation.accepted_at:
                # Resending to same email doesn't count as new member
                return

            current_members = await db.user_workspaces.get_workspace_members(
                workspace_id
            )
            pending_invitations = await db.invitations.get_by_workspace(
                workspace_id, include_accepted=False
            )

            total_members = len(current_members)
            total_pending = len(
                [
                    inv
                    for inv in pending_invitations
                    if inv.invitation_type != InvitationType.OWNERSHIP_TRANSFER.value
                ]
            )
            total_count = total_members + total_pending

            if total_count >= features.max_team_members:
                raise SubscriptionLimitError(
                    f"Team member limit reached. Your plan allows {features.max_team_members} member(s), "
                    f"and you currently have {total_count} (including pending invitations). "
                    "Please upgrade your plan to add more team members.",
                )

    def apply_tier_defaults(
        self,
        compose_file: "ComposeFile",
        features: BaseFeatures,
    ) -> None:
        """Apply tier-appropriate resource defaults to services that don't specify resources."""
        for service in compose_file.services:
            # Apply CPU default if not specified
            if service.deploy.resources.limits.cpus is None:
                service.deploy.resources.limits.cpus = str(features.max_cpu_per_service)

            # Apply memory default if not specified
            if service.deploy.resources.limits.memory is None:
                service.deploy.resources.limits.memory = (
                    f"{features.max_memory_per_service}G"
                )

    async def check_deployment_features(
        self,
        compose_file: ComposeFile,
        features: BaseFeatures,
    ) -> None:
        """Check if deployment features are within subscription limits.

        Checks:
        - Replicas per service (auto-scaling limit)
        - CPU per service
        - Memory per service
        - Custom domains enabled

        Note: Services, volumes, and networks per deployment are NOT limited.
        Usage-based billing handles the cost of these resources.
        """

        # Get custom domains from parsed services
        custom_domains = [
            service.domain for service in compose_file.services if service.domain
        ]

        # Check replicas per service (auto-scaling limit)
        max_replicas = features.max_replicas_per_service
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
                    f"Auto-scaling limit exceeded for service '{service.name}'. Your plan allows up to {max_replicas}x scaling, "
                    f"but HPA max is set to {service.scaling.max}. Please upgrade your plan or reduce the max replicas.",
                )

        # Check CPU limits
        for service in compose_file.services:
            cpu_str = service.deploy.resources.limits.cpus
            if cpu_str is not None:
                # Parse CPU value (handles formats like "0.5", "2", "500m")
                cpu_cores = parse_cpu_to_cores(cpu_str)

                if cpu_cores > features.max_cpu_per_service:
                    raise SubscriptionLimitError(
                        f"CPU limit exceeded for service '{service.name}'. "
                        f"Your plan allows {features.max_cpu_per_service} CPU cores per service, "
                        f"but this service requests {cpu_cores} cores. "
                        "Please upgrade your plan or reduce CPU allocation."
                    )

        # Check Memory limits
        for service in compose_file.services:
            memory_str = service.deploy.resources.limits.memory
            if memory_str is not None:
                # Parse memory value to GB (handles formats like "512Mi", "2Gi", "1G", "1024M")
                memory_gb = parse_memory_to_gb(memory_str)

                if memory_gb > features.max_memory_per_service:
                    raise SubscriptionLimitError(
                        f"Memory limit exceeded for service '{service.name}'. "
                        f"Your plan allows {features.max_memory_per_service}GB per service, "
                        f"but this service requests {memory_gb:.2f}GB. "
                        "Please upgrade your plan or reduce memory allocation."
                    )

        # Check custom domains
        if not features.custom_domains_enabled and len(custom_domains) > 0:
            raise SubscriptionLimitError(
                "Custom domains are not available on your plan. Please upgrade to a plan that supports custom domains.",
            )

    async def validate_workspace_for_owner(
        self, workspace_id: str, new_owner_features: BaseFeatures, new_owner_id: str
    ) -> None:
        """Validate that a workspace can be transferred to a new owner based on their plan features.

        Checks:
        - New owner's total deployment count + workspace's deployments <= limit
        - Each deployment's features (CPU, memory, replicas, domains) are within new owner's limits
        """
        # Get new owner's current total deployment count
        async with get_db_context() as db:
            current_deployments = (
                await db.compose_deployments.get_total_deployment_count_for_user(
                    new_owner_id
                )
            )

        # Get deployment count in the workspace being transferred
        async with get_db_context() as db:
            transfer_deployment_count = (
                await db.compose_deployments.get_deployment_count(workspace_id)
            )

        # Check if accepting this workspace would exceed deployment limit
        total_after_transfer = current_deployments + transfer_deployment_count
        if total_after_transfer > new_owner_features.deployment_limit:
            raise SubscriptionLimitError(
                f"Deployment limit would be exceeded. Your plan allows {new_owner_features.deployment_limit} deployment(s), "
                f"you currently have {current_deployments}, and this workspace has {transfer_deployment_count}. "
                "Please upgrade your plan to accept this workspace transfer.",
            )

        # Validate each deployment's features against new owner's plan
        async with get_db_context() as db:
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
                await self.check_deployment_features(compose_file, new_owner_features)

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
