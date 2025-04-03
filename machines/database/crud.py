from machines.database.session import session_manager
from machines.database.base import Base
from machines.config import app_config
from machines.database.utils import generate_token_expires_at, token_is_expired
from machines.database.tokens import TokenService, TokenPydantic, TokenRole, TokenExpiration


async def create_tables():
    async with session_manager.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def update_admin_tokens():
    token_service = TokenService()

    # check if the admin token exists
    admin_tokens = await token_service.afind(filters={"token": app_config.ADMIN_TOKEN})
    token_exists = False
    if admin_tokens and len(admin_tokens) > 0:
        # delete tokens if they are expired
        for token in admin_tokens:
            if token.id and token_is_expired(token.expires_at):
                print(f"Deleting expired token: {token.token}")
                await token_service.adelete(token.id)

            else:
                token_exists = True

    if token_exists:
        print("Admin token already exists. Skipping replacement...")
        return

    token = TokenPydantic(
        user_id="admin",
        token=app_config.ADMIN_TOKEN,
        expires_at=generate_token_expires_at(TokenExpiration.THREE_HUNDRED_SIXTY_FIVE_DAYS),
        role=TokenRole.ADMIN,
    )

    new_token = await token_service.acreate(token)
    if new_token and new_token.id:
        print(f"New admin token created: {new_token.token}")
