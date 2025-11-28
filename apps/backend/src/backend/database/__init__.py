from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.api_keys import ApiKeyService
from backend.database.compose import ComposeDeploymentService
from backend.database.invitations import WorkspaceInvitationService
from backend.database.secrets import SecretService
from backend.database.session import session_manager
from backend.database.usage import UsageService
from backend.database.user_workspaces import UserWorkspaceService
from backend.database.users import UserService
from backend.database.workspaces import WorkspaceService


@dataclass
class Database:
    api_keys: ApiKeyService
    users: UserService
    compose_deployments: ComposeDeploymentService
    secrets: SecretService
    workspaces: WorkspaceService
    user_workspaces: UserWorkspaceService
    usage: UsageService
    invitations: WorkspaceInvitationService


def _create_database(session: AsyncSession) -> Database:
    return Database(
        api_keys=ApiKeyService(session),
        users=UserService(session),
        compose_deployments=ComposeDeploymentService(session),
        secrets=SecretService(session),
        workspaces=WorkspaceService(session),
        user_workspaces=UserWorkspaceService(session),
        usage=UsageService(session),
        invitations=WorkspaceInvitationService(session),
    )


async def get_db() -> AsyncGenerator[Database, None]:
    """FastAPI dependency - use with Depends(get_db)."""
    async with session_manager.get_session() as session:
        try:
            yield _create_database(session)
            await session.commit()

        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def get_db_context() -> AsyncGenerator[Database, None]:
    """Context manager for Prefect tasks and scripts - use with async with."""
    async with session_manager.get_session() as session:
        try:
            yield _create_database(session)
            await session.commit()

        except Exception:
            await session.rollback()
            raise


__all__ = ["Database", "get_db", "get_db_context"]
