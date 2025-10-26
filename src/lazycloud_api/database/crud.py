import uuid
from datetime import datetime, timedelta, timezone

from loguru import logger

from lazycloud_api.config import app_config
from lazycloud_api.database.api_keys import (
    ApiKeyExpirationDays,
    ApiKeyPydantic,
    ApiKeyService,
)
from lazycloud_api.database.base import Base
from lazycloud_api.database.session import session_manager
from lazycloud_api.database.user_workspaces import (
    UserWorkspacePydantic,
    UserWorkspaceService,
    UserWorkspaceStatus,
    WorkspaceRole,
)
from lazycloud_api.database.users import (
    UserPydantic,
    UserRole,
    UserService,
    UserStatus,
)
from lazycloud_api.database.utils import api_key_is_expired, generate_api_key_expires_at
from lazycloud_api.database.workspaces import WorkspacePydantic, WorkspaceService
from lazycloud_api.services import get_polar_service


async def create_tables():
    async with session_manager.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def update_admin_api_keys():
    api_key_service = ApiKeyService()

    # check if we have an admin user
    user_service = UserService()
    user = await user_service.aget_by_clerk_id(clerk_id="lzy_admin")

    if not user:
        # Create admin user with workspace in a transaction
        logger.info("Creating admin user and workspace...")
        async with user_service.transaction() as session:
            user = UserPydantic(
                id=uuid.uuid4(),
                name="admin user",
                email="admin@lazycloud.com",
                clerk_id="lzy_admin",
                role=UserRole.ADMIN,
                status=UserStatus.ACTIVE,
            )
            user = await user_service.acreate(user, session=session)

            # Create personal workspace
            workspace_service = WorkspaceService()
            personal_workspace = WorkspacePydantic(
                name="Personal",
                is_personal=True,
            )
            personal_workspace = await workspace_service.acreate(
                personal_workspace, session=session
            )

            # Link admin user to their personal workspace as owner
            user_workspace_service = UserWorkspaceService()
            user_workspace_membership = UserWorkspacePydantic(
                user_id=user.id,
                workspace_id=personal_workspace.id,
                role=WorkspaceRole.OWNER,
                status=UserWorkspaceStatus.ACTIVE,
            )
            await user_workspace_service.acreate(
                user_workspace_membership, session=session
            )

        logger.info(f"Admin user and workspace created: {personal_workspace.id}")

    else:
        # ensure admin user has a personal workspace
        workspace_service = WorkspaceService()
        personal_workspace = await workspace_service.aget_personal_workspace(user.id)
        if not personal_workspace:
            logger.info("Creating personal workspace for admin user...")
            async with workspace_service.transaction() as session:
                personal_workspace = WorkspacePydantic(
                    name="Personal",
                    is_personal=True,
                )
                personal_workspace = await workspace_service.acreate(
                    personal_workspace, session=session
                )

                # link admin user to their personal workspace as owner
                user_workspace_service = UserWorkspaceService()
                user_workspace_membership = UserWorkspacePydantic(
                    user_id=user.id,
                    workspace_id=personal_workspace.id,
                    role=WorkspaceRole.OWNER,
                    status=UserWorkspaceStatus.ACTIVE,
                )
                await user_workspace_service.acreate(
                    user_workspace_membership, session=session
                )
            logger.info(
                f"Personal workspace created for admin user: {personal_workspace.id}"
            )

        else:
            logger.info(
                f"Personal workspace already exists for admin user: {personal_workspace.id}"
            )

    # check if the admin api key exists
    admin_api_keys = await api_key_service.aget_by_user_id(user_id=user.id)
    api_key_exists = False
    if admin_api_keys and len(admin_api_keys) > 0:
        # delete api keys if they are expired
        for api_key in admin_api_keys:
            if (
                api_key.user_id == user.id
                and api_key.updated_at
                and api_key.value != app_config.ADMIN_API_KEY
            ):
                if api_key_is_expired(api_key.expires_at):
                    logger.info(f"Deleting expired admin api key: {api_key.value}")
                    await api_key_service.adelete(api_key.id)
                    continue

                elif api_key.updated_at > datetime.now(timezone.utc) - timedelta(
                    minutes=30
                ):
                    # if the api key is not expired, set it to expire in 30 min.
                    # this gives time to refresh the api key on the frontend
                    # if the api key was updated in the last 30 minutes, we don't need to update it
                    api_key.expires_at = generate_api_key_expires_at(
                        ApiKeyExpirationDays.THIRTY_DAYS
                    )
                    logger.info(
                        f"Updating admin api key: {api_key.value} to expire in 30 days"
                    )
                    await api_key_service.aupdate(api_key)

            else:
                api_key_exists = True

    if api_key_exists:
        logger.info("Admin api key already exists. Skipping replacement...")
        return

    api_key = ApiKeyPydantic(
        name="lzy_admin_api_key",
        user_id=user.id,
        value=app_config.ADMIN_API_KEY,
        expires_at=generate_api_key_expires_at(
            ApiKeyExpirationDays.THREE_HUNDRED_SIXTY_FIVE_DAYS
        ),
    )

    new_api_key = await api_key_service.acreate(api_key)
    if new_api_key and new_api_key.user_id == user.id:
        logger.info(f"New admin api key created: {new_api_key.value}")

    # create a new customer in Polar (outside transaction)
    polar_service = get_polar_service()

    try:
        # check if the customer already exists (idempotency)
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
        # Don't fail the entire process for external service issues
