"""Database fixtures for integration tests using dependency injection container."""

import socket
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from lazycloud_api.database import Database, _create_database
from lazycloud_api.database.api_keys import ApiKeyPydantic
from lazycloud_api.database.compose import ComposeDeploymentPydantic
from lazycloud_api.database.invitations import WorkspaceInvitationPydantic
from lazycloud_api.database.secrets import SecretPydantic
from lazycloud_api.database.session import session_manager
from lazycloud_api.database.usage import UsageRecordPydantic
from lazycloud_api.database.user_workspaces import UserWorkspacePydantic
from lazycloud_api.database.users import (
    SubscriptionState,
    UserPydantic,
    UserRole,
    UserStatus,
)
from lazycloud_api.database.workspaces import WorkspacePydantic, WorkspaceStatus
from shared.models.billing import UsageRecordStatus, UsageRecordType
from shared.models.deployments import DeploymentStates
from shared.models.secrets import SecretSource, SecretState
from shared.models.workspaces import InvitationType, UserWorkspaceStatus, WorkspaceRole


def is_db_available() -> bool:
    """Check if the test database is available."""
    try:
        sock = socket.create_connection(("localhost", 5432), timeout=1)
        sock.close()
        return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False


requires_db = pytest.mark.skipif(
    not is_db_available(),
    reason="Database not available (run: docker compose up -d postgres)",
)


@pytest.fixture
async def db_session() -> AsyncSession:
    """Provide a transactional session that rolls back after each test."""
    # Reset session_manager to get a fresh engine for this event loop
    await session_manager.reset()
    session_manager._ensure_initialized()
    engine = session_manager.engine

    # Get a raw connection from the engine
    conn = await engine.connect()
    try:
        # Start a transaction
        trans = await conn.begin()
        try:
            # Create session bound to this connection
            session = AsyncSession(
                bind=conn,
                expire_on_commit=False,
            )
            try:
                yield session
            finally:
                await session.close()
        finally:
            # Always rollback - no data persists between tests
            await trans.rollback()
    finally:
        await conn.close()
        # Clean up the engine after the test
        await session_manager.reset()


@pytest.fixture
def db(db_session: AsyncSession) -> Database:
    """Get Database instance bound to the test session."""
    return _create_database(db_session)


# Helper functions to create test data models (not persisted)


def make_user(unique_id: str | None = None) -> UserPydantic:
    """Create a UserPydantic model (not persisted)."""
    unique_id = unique_id or str(uuid.uuid4())[:8]
    return UserPydantic(
        name=f"Test User {unique_id}",
        email=f"test_{unique_id}@example.com",
        clerk_id=f"clerk_test_{unique_id}",
        role=UserRole.USER,
        status=UserStatus.ACTIVE,
        subscription_state=SubscriptionState.WITHIN_LIMITS,
    )


def make_admin_user(unique_id: str | None = None) -> UserPydantic:
    """Create an admin UserPydantic model (not persisted)."""
    unique_id = unique_id or str(uuid.uuid4())[:8]
    return UserPydantic(
        name=f"Admin User {unique_id}",
        email=f"admin_{unique_id}@example.com",
        clerk_id=f"clerk_admin_{unique_id}",
        role=UserRole.ADMIN,
        status=UserStatus.ACTIVE,
        subscription_state=SubscriptionState.WITHIN_LIMITS,
    )


def make_workspace(
    name: str | None = None, is_personal: bool = False
) -> WorkspacePydantic:
    """Create a WorkspacePydantic model (not persisted)."""
    unique_id = str(uuid.uuid4())[:8]
    return WorkspacePydantic(
        name=name or f"Test Workspace {unique_id}",
        is_personal=is_personal,
        status=WorkspaceStatus.ACTIVE,
    )


def make_user_workspace(
    user_id: str,
    workspace_id: str,
    role: WorkspaceRole = WorkspaceRole.OWNER,
) -> UserWorkspacePydantic:
    """Create a UserWorkspacePydantic model (not persisted)."""
    return UserWorkspacePydantic(
        user_id=user_id,
        workspace_id=workspace_id,
        role=role,
        status=UserWorkspaceStatus.ACTIVE,
    )


def make_deployment(
    workspace_id: str,
    name: str | None = None,
    state: DeploymentStates = DeploymentStates.DEPLOYED,
) -> ComposeDeploymentPydantic:
    """Create a ComposeDeploymentPydantic model (not persisted)."""
    unique_id = str(uuid.uuid4())[:8]
    return ComposeDeploymentPydantic(
        name=name or f"test-deployment-{unique_id}",
        namespace=f"lc-test-{unique_id}",
        workspace_id=workspace_id,
        compose_yaml="version: '3.8'\nservices:\n  web:\n    image: nginx",
        state=state,
    )


def make_api_key(
    user_id: str,
    name: str | None = None,
    expires_in_days: int = 30,
) -> ApiKeyPydantic:
    """Create an ApiKeyPydantic model (not persisted)."""
    unique_id = str(uuid.uuid4())[:8]
    return ApiKeyPydantic(
        name=name or f"test-key-{unique_id}",
        user_id=user_id,
        value=f"lzy_test_{unique_id}_{uuid.uuid4().hex[:16]}",
        expires_at=datetime.now(timezone.utc) + timedelta(days=expires_in_days),
    )


def make_secret(
    deployment_id: str,
    key: str | None = None,
    value: str = "secret_value",
    source: SecretSource = SecretSource.USER,
    state: SecretState = SecretState.DEPLOYED,
) -> SecretPydantic:
    """Create a SecretPydantic model (not persisted)."""
    unique_id = str(uuid.uuid4())[:8]
    return SecretPydantic(
        deployment_id=deployment_id,
        key=key or f"SECRET_{unique_id}",
        value=value,
        source=source,
        state=state,
    )


def make_invitation(
    workspace_id: str,
    invited_by_user_id: str,
    email: str | None = None,
    role: WorkspaceRole = WorkspaceRole.MEMBER,
    expires_in_days: int = 7,
) -> WorkspaceInvitationPydantic:
    """Create a WorkspaceInvitationPydantic model (not persisted)."""
    unique_id = str(uuid.uuid4())[:8]
    return WorkspaceInvitationPydantic(
        workspace_id=workspace_id,
        email=email or f"invite_{unique_id}@example.com",
        role=role,
        token=f"inv_{uuid.uuid4().hex}",
        invited_by_user_id=invited_by_user_id,
        expires_at=datetime.now(timezone.utc) + timedelta(days=expires_in_days),
        invitation_type=InvitationType.MEMBER.value,
    )


def make_usage_record(
    workspace_id: str,
    collection_start: datetime | None = None,
    collection_end: datetime | None = None,
    cpu_core_seconds: float = 0.0,
    memory_gb_seconds: float = 0.0,
    storage_gb_hours: float = 0.0,
) -> UsageRecordPydantic:
    """Create a UsageRecordPydantic model (not persisted)."""
    now = datetime.now(timezone.utc)
    return UsageRecordPydantic(
        workspace_id=workspace_id,
        record_type=UsageRecordType.HOURLY.value,
        status=UsageRecordStatus.DRAFT,
        collection_start=collection_start or now - timedelta(hours=1),
        collection_end=collection_end or now,
        cpu_core_seconds=cpu_core_seconds,
        memory_gb_seconds=memory_gb_seconds,
        storage_gb_hours=storage_gb_hours,
        standard_gb_hours=0.0,
        shared_gb_hours=0.0,
        build_minutes=0.0,
        public_endpoint_hours=0.0,
    )


# Convenience fixtures that create data in the database


@pytest.fixture
async def db_user(db: Database) -> UserPydantic:
    """Create a test user in the database (rolled back after test)."""
    return await db.users.create(make_user())


@pytest.fixture
async def db_admin_user(db: Database) -> UserPydantic:
    """Create an admin test user in the database."""
    return await db.users.create(make_admin_user())


@pytest.fixture
async def db_workspace(db: Database, db_user: UserPydantic) -> WorkspacePydantic:
    """Create a test workspace linked to the test user."""
    workspace = await db.workspaces.create(make_workspace())
    await db.user_workspaces.create(make_user_workspace(db_user.id, workspace.id))
    return workspace


@pytest.fixture
async def db_personal_workspace(
    db: Database, db_user: UserPydantic
) -> WorkspacePydantic:
    """Create a personal workspace for the test user."""
    workspace = await db.workspaces.create(
        make_workspace(name="Personal", is_personal=True)
    )
    await db.user_workspaces.create(make_user_workspace(db_user.id, workspace.id))
    return workspace


@pytest.fixture
async def db_deployment(
    db: Database, db_workspace: WorkspacePydantic
) -> ComposeDeploymentPydantic:
    """Create a test deployment in the database."""
    return await db.compose_deployments.create(make_deployment(db_workspace.id))


@pytest.fixture
async def db_api_key(db: Database, db_user: UserPydantic) -> ApiKeyPydantic:
    """Create a test API key in the database."""
    return await db.api_keys.create(make_api_key(db_user.id))


@pytest.fixture
async def db_secret(
    db: Database, db_deployment: ComposeDeploymentPydantic
) -> SecretPydantic:
    """Create a test secret in the database."""
    return await db.secrets.create(make_secret(db_deployment.id))


@pytest.fixture
async def db_invitation(
    db: Database, db_workspace: WorkspacePydantic, db_user: UserPydantic
) -> WorkspaceInvitationPydantic:
    """Create a test invitation in the database."""
    return await db.invitations.create(make_invitation(db_workspace.id, db_user.id))


@pytest.fixture
async def db_multiple_workspaces(
    db: Database, db_user: UserPydantic
) -> list[WorkspacePydantic]:
    """Create multiple workspaces for limit testing."""
    workspaces = []
    for i in range(3):
        workspace = await db.workspaces.create(make_workspace(is_personal=(i == 0)))
        await db.user_workspaces.create(make_user_workspace(db_user.id, workspace.id))
        workspaces.append(workspace)
    return workspaces


@pytest.fixture
async def db_multiple_deployments(
    db: Database, db_workspace: WorkspacePydantic
) -> list[ComposeDeploymentPydantic]:
    """Create multiple deployments for limit testing."""
    deployments = []
    for i in range(3):
        deployment = await db.compose_deployments.create(
            make_deployment(db_workspace.id, name=f"deployment-{i}")
        )
        deployments.append(deployment)
    return deployments
