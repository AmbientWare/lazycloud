from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.api_keys import ApiKeyService
from backend.database.billing_audit import BillingAuditService
from backend.database.compose import ComposeDeploymentService
from backend.database.invitations import WorkspaceInvitationService
from backend.database.secrets import SecretService
from backend.database.session import session_manager
from backend.database.usage import UsageService
from backend.database.user_workspaces import UserWorkspaceService
from backend.database.users import UserService
from backend.database.workspaces import WorkspaceService

_current_db: ContextVar["Database | None"] = ContextVar("_current_db", default=None)


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
    billing_audit: BillingAuditService


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
        billing_audit=BillingAuditService(session),
    )


async def get_db() -> AsyncGenerator[Database, None]:
    """FastAPI dependency - use with Depends(get_db)."""
    async with session_manager.get_session() as session:
        db = _create_database(session)
        token = _current_db.set(db)
        try:
            yield db
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
        finally:
            _current_db.reset(token)


@asynccontextmanager
async def get_db_context() -> AsyncGenerator[Database, None]:
    """Context manager for background tasks and scripts - use with async with."""
    existing = _current_db.get()
    if existing is not None:
        yield existing
        return

    async with session_manager.get_session() as session:
        db = _create_database(session)
        token = _current_db.set(db)
        try:
            yield db
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
        finally:
            _current_db.reset(token)


__all__ = ["Database", "get_db", "get_db_context"]
