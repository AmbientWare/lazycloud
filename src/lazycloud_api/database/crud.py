import uuid
from datetime import datetime, timedelta, timezone

from loguru import logger

from lazycloud_api.config import app_config
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
from lazycloud_api.database.utils import api_key_is_expired, generate_api_key_expires_at
from lazycloud_api.database.workspaces import WorkspacePydantic
from lazycloud_api.services import get_polar_service


async def update_admin_api_keys():
    """Create or update admin user and API keys."""
    async with get_db_context() as db:
        # Check if we have an admin user
        user = await db.users.get_by_clerk_id(clerk_id="lzy_admin")

        if not user:
            # Create admin user with workspace
            logger.info("Creating admin user and workspace...")
            user = UserPydantic(
                id=uuid.uuid4(),
                name="admin user",
                email="admin@lazycloud.com",
                clerk_id="lzy_admin",
                role=UserRole.ADMIN,
                status=UserStatus.ACTIVE,
                subscription_state=SubscriptionState.WITHIN_LIMITS,
            )
            user = await db.users.create(user)

            # Create personal workspace
            personal_workspace = WorkspacePydantic(
                name="Personal",
                is_personal=True,
            )
            personal_workspace = await db.workspaces.create(personal_workspace)

            # Link admin user to their personal workspace as owner
            user_workspace_membership = UserWorkspacePydantic(
                user_id=user.id,
                workspace_id=personal_workspace.id,
                role=WorkspaceRole.OWNER,
                status=UserWorkspaceStatus.ACTIVE,
            )
            await db.user_workspaces.create(user_workspace_membership)
            logger.info(f"Admin user and workspace created: {personal_workspace.id}")

        else:
            # Ensure admin user has a personal workspace
            personal_workspace = await db.workspaces.get_personal_workspace(user.id)
            if not personal_workspace:
                logger.info("Creating personal workspace for admin user...")
                personal_workspace = WorkspacePydantic(
                    name="Personal",
                    is_personal=True,
                )
                personal_workspace = await db.workspaces.create(personal_workspace)

                # Link admin user to their personal workspace as owner
                user_workspace_membership = UserWorkspacePydantic(
                    user_id=user.id,
                    workspace_id=personal_workspace.id,
                    role=WorkspaceRole.OWNER,
                    status=UserWorkspaceStatus.ACTIVE,
                )
                await db.user_workspaces.create(user_workspace_membership)
                logger.info(
                    f"Personal workspace created for admin user: {personal_workspace.id}"
                )
            else:
                logger.info(
                    f"Personal workspace already exists for admin user: {personal_workspace.id}"
                )

        # Check if the admin api key exists
        admin_api_keys = await db.api_keys.get_by_user_id(user_id=user.id)
        api_key_exists = False
        if admin_api_keys and len(admin_api_keys) > 0:
            # Delete api keys if they are expired
            for api_key in admin_api_keys:
                if (
                    api_key.user_id == user.id
                    and api_key.updated_at
                    and api_key.value != app_config.ADMIN_API_KEY
                ):
                    if api_key_is_expired(api_key.expires_at):
                        logger.info(f"Deleting expired admin api key: {api_key.value}")
                        await db.api_keys.delete(api_key.id)
                        continue

                    elif api_key.updated_at > datetime.now(timezone.utc) - timedelta(
                        minutes=30
                    ):
                        api_key.expires_at = generate_api_key_expires_at(
                            ApiKeyExpirationDays.THIRTY_DAYS
                        )
                        logger.info(
                            f"Updating admin api key: {api_key.value} to expire in 30 days"
                        )
                        await db.api_keys.update(api_key)

                else:
                    api_key_exists = True

        if api_key_exists:
            logger.info("Admin api key already exists. Skipping replacement...")
            return user

        api_key = ApiKeyPydantic(
            name="lzy_admin_api_key",
            user_id=user.id,
            value=app_config.ADMIN_API_KEY,
            expires_at=generate_api_key_expires_at(
                ApiKeyExpirationDays.THREE_HUNDRED_SIXTY_FIVE_DAYS
            ),
        )

        new_api_key = await db.api_keys.create(api_key)
        if new_api_key and new_api_key.user_id == user.id:
            logger.info(f"New admin api key created: {new_api_key.value}")

    polar_service = get_polar_service()

    try:
        customer = await polar_service.customers.get_customer(external_id=user.clerk_id)
        if not customer:
            customer = await polar_service.customers.create_customer(
                email=user.email,
                external_id=user.clerk_id,
                name=user.name,
                metadata={"user_id": user.id},
            )
            if customer:
                logger.info(f"Created Polar customer for admin user: {user.clerk_id}")
        else:
            logger.info(
                f"Polar customer already exists for admin user: {user.clerk_id}"
            )

    except Exception as e:
        logger.error(f"Failed to create Polar customer for admin user: {e}")
