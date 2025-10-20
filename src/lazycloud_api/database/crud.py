from datetime import datetime, timedelta, timezone

from lazycloud_api.config import app_config
from lazycloud_api.database.api_keys import (
    ApiKeyExpirationDays,
    ApiKeyPydantic,
    ApiKeyService,
)
from lazycloud_api.database.base import Base
from lazycloud_api.database.session import session_manager
from lazycloud_api.database.users import (
    UserPydantic,
    UserRole,
    UserService,
    UserStatus,
)
from lazycloud_api.database.utils import api_key_is_expired, generate_api_key_expires_at


async def create_tables():
    async with session_manager.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def update_admin_api_keys():
    api_key_service = ApiKeyService()

    # check if we have an admin user
    user_service = UserService()
    user = await user_service.aget_by_clerk_id(clerk_id="lzy_admin")
    if not user:
        user = UserPydantic(
            clerk_id="lzy_admin",
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
        )
        user = await user_service.acreate(user)

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
                    print(f"Deleting expired admin api key: {api_key.value}")
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
                    print(
                        f"Updating admin api key: {api_key.value} to expire in 30 days"
                    )
                    await api_key_service.aupdate(api_key)

            else:
                api_key_exists = True

    if api_key_exists:
        print("Admin api key already exists. Skipping replacement...")
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
        print(f"New admin api key created: {new_api_key.value}")
