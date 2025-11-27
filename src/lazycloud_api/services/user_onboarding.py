from typing import Optional

from loguru import logger

from lazycloud_api.database import get_db_context
from lazycloud_api.database.api_keys import ApiKeyExpirationDays, ApiKeyPydantic
from lazycloud_api.database.user_workspaces import (
    UserWorkspacePydantic,
    UserWorkspaceStatus,
    WorkspaceRole,
)
from lazycloud_api.database.users import (
    SubscriptionState,
    UserPydantic,
    UserRole,
    UserStatus,
)
from lazycloud_api.database.utils import generate_api_key, generate_api_key_expires_at
from lazycloud_api.database.workspaces import WorkspacePydantic
from lazycloud_api.services import PolarService


class UserOnboardingService:
    """Service for handling user onboarding with proper transaction management."""

    def __init__(self, polar_service: PolarService):
        self.polar_service = polar_service

    async def onboard_user(self, clerk_id: str, name: str, email: str) -> UserPydantic:
        """Onboard a new user with proper transaction handling."""

        # Check if user already exists
        existing_user = await self._get_user_by_clerk_id(clerk_id)
        if existing_user:
            raise ValueError(f"User with clerk_id {clerk_id} already exists")

        # Create user with all related entities in a transaction
        user = await self._create_user_with_entities(clerk_id, name, email)

        # Create Polar customer AFTER transaction commits (external service)
        await self._create_polar_customer(user)

        return user

    async def _get_user_by_clerk_id(self, clerk_id: str) -> Optional[UserPydantic]:
        """Get user by clerk_id."""
        async with get_db_context() as db:
            return await db.users.get_by_clerk_id(clerk_id=clerk_id)

    async def _create_user_with_entities(
        self, clerk_id: str, name: str, email: str
    ) -> UserPydantic:
        """Create user and all related entities in a single transaction."""
        try:
            # All database operations in one atomic transaction
            async with get_db_context() as db:
                # Create user
                user = UserPydantic(
                    name=name,
                    email=email,
                    clerk_id=clerk_id,
                    role=UserRole.ADMIN,
                    status=UserStatus.ACTIVE,
                    subscription_state=SubscriptionState.WITHIN_LIMITS,
                )
                user = await db.users.create(user)

                # Create API key
                api_key = ApiKeyPydantic(
                    name="default",
                    user_id=user.id,
                    value=generate_api_key(),
                    expires_at=generate_api_key_expires_at(ApiKeyExpirationDays.NEVER),
                )
                await db.api_keys.create(api_key)

                # Create personal workspace
                workspace = WorkspacePydantic(
                    name="Personal",
                    is_personal=True,
                )
                workspace = await db.workspaces.create(workspace)

                # Link user to workspace
                user_workspace = UserWorkspacePydantic(
                    user_id=user.id,
                    workspace_id=workspace.id,
                    role=WorkspaceRole.OWNER,
                    status=UserWorkspaceStatus.ACTIVE,
                )
                await db.user_workspaces.create(user_workspace)

            logger.info(f"Successfully created user {clerk_id} in database")
            return user

        except Exception as e:
            logger.error(f"Failed to onboard user {clerk_id}: {e}")
            raise

    async def _create_polar_customer(self, user: UserPydantic) -> None:
        """Create customer in Polar (external service)."""
        try:
            # Check if customer already exists (idempotency)
            existing_customer = await self.polar_service.customers.get_customer(
                external_id=user.clerk_id
            )
            if existing_customer:
                logger.info(f"Polar customer already exists for {user.clerk_id}")
                return

            customer = await self.polar_service.customers.create_customer(
                email=user.email,
                external_id=user.clerk_id,
                name=user.name,
                metadata={"clerk_id": user.clerk_id, "user_id": user.id},
            )

            if not customer:
                logger.warning(f"Failed to create Polar customer for {user.clerk_id}")

            else:
                logger.info(f"Created Polar customer for {user.clerk_id}")

        except Exception as e:
            logger.error(f"Failed to create Polar customer for {user.clerk_id}: {e}")
            # Don't fail the entire onboarding for external service issues
            # This is a common pattern - external services are best-effort
