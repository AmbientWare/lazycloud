from machines.database.session import session_manager
from machines.database.base import Base
from machines.config import app_config
from machines.database.utils import generate_token_expires_at, token_is_expired
from machines.database.tokens import (
    TokenService,
    TokenPydantic,
    TokenRole,
    TokenExpirationMinutes,
    TokenExpirationDays,
)
from datetime import datetime, timezone, timedelta


async def create_tables():
    async with session_manager.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def update_admin_tokens():
    token_service = TokenService()

    # check if the admin token exists
    admin_tokens = await token_service.afind(filters={"user_id": "admin"})
    token_exists = False
    if admin_tokens and len(admin_tokens) > 0:
        # delete tokens if they are expired
        for token in admin_tokens:
            if token.id and token.updated_at and token.token != app_config.ADMIN_TOKEN:
                if token_is_expired(token.expires_at):
                    print(f"Deleting expired admin token: {token.token}")
                    await token_service.adelete(token.id)
                    continue

                elif token.updated_at > datetime.now(timezone.utc) - timedelta(
                    minutes=30
                ):
                    # if the token is not expired, set it to expire in 30 min.
                    # this gives time to refresh the token on the frontend
                    # if the token was updated in the last 30 minutes, we don't need to update it
                    token.expires_at = generate_token_expires_at(
                        TokenExpirationMinutes.THIRTY_MINUTES
                    )
                    print(
                        f"Updating admin token: {token.token} to expire in 30 minutes"
                    )
                    await token_service.aupdate(token)

            else:
                token_exists = True

    if token_exists:
        print("Admin token already exists. Skipping replacement...")
        return

    token = TokenPydantic(
        user_id="admin",
        token=app_config.ADMIN_TOKEN,
        expires_at=generate_token_expires_at(
            TokenExpirationDays.THREE_HUNDRED_SIXTY_FIVE_DAYS
        ),
        role=TokenRole.ADMIN,
    )

    new_token = await token_service.acreate(token)
    if new_token and new_token.id:
        print(f"New admin token created: {new_token.token}")
