"""Database fixtures for integration tests using dependency injection container."""

import socket
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from backend.billing.product_details.features import BaseFeatures
from backend.database import Database, _create_database
from backend.database.api_keys import ApiKeyPydantic
from backend.database.compose import ComposeDeploymentPydantic
from backend.database.invitations import WorkspaceInvitationPydantic
from backend.database.secrets import SecretPydantic
from backend.database.session import session_manager
from backend.database.usage import DailyUsageRecordPydantic, DailyUsageStatus
from backend.database.user_workspaces import UserWorkspacePydantic
from backend.database.users import (
    SubscriptionState,
    UserPydantic,
    UserRole,
    UserStatus,
)
from backend.database.workspaces import WorkspacePydantic, WorkspaceStatus
from models.deployments import DeploymentStates
from models.helm import ImageConfig, ServiceValues
from models.secrets import SecretSource, SecretState
from models.workspaces import InvitationType, UserWorkspaceStatus, WorkspaceRole
from sqlalchemy.ext.asyncio import AsyncSession


def is_db_available() -> bool:
    """Check if the test database is available."""
    try:
        sock = socket.create_connection(("localhost", 6432), timeout=1)
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
        workos_id=f"workos_test_{unique_id}",
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
        workos_id=f"workos_admin_{unique_id}",
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


def make_daily_usage_record(
    workspace_id: str,
    usage_date: datetime | None = None,
    cpu_core_seconds: float = 0.0,
    memory_gb_seconds: float = 0.0,
    standard_gb_hours: float = 0.0,
    shared_gb_hours: float = 0.0,
    build_minutes: float = 0.0,
    public_endpoint_hours: float = 0.0,
) -> DailyUsageRecordPydantic:
    """Create a DailyUsageRecordPydantic model (not persisted)."""
    return DailyUsageRecordPydantic(
        workspace_id=workspace_id,
        usage_date=(usage_date or datetime.now(timezone.utc)).date(),
        status=DailyUsageStatus.COLLECTING,
        cpu_core_seconds=cpu_core_seconds,
        memory_gb_seconds=memory_gb_seconds,
        standard_gb_hours=standard_gb_hours,
        shared_gb_hours=shared_gb_hours,
        build_minutes=build_minutes,
        public_endpoint_hours=public_endpoint_hours,
        intervals_collected=0,
        expected_intervals=96,
    )


def make_features(
    deployment_limit: int = 10,
    max_team_members: int | None = 10,
    max_cpu_per_service: float = 8.0,
    max_memory_per_service: int = 16,
    max_replicas: int = 10,
    custom_domains_enabled: bool = True,
    support_level: str = "email",
) -> BaseFeatures:
    """Create test subscription features with specified limits."""
    return BaseFeatures(
        deployment_limit=deployment_limit,
        max_team_members=max_team_members,
        max_cpu_per_service=max_cpu_per_service,
        max_memory_per_service=max_memory_per_service,
        max_replicas_per_service=max_replicas,
        custom_domains_enabled=custom_domains_enabled,
        support_level=support_level,
    )


def make_service(name: str) -> ServiceValues:
    """Create a ServiceValues with default image."""
    return ServiceValues(
        name=name,
        enabled=True,
        resourceName=name,
        image=ImageConfig(repository="nginx", tag="latest", pullPolicy="IfNotPresent"),
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
async def db_user_with_workspace(
    db: Database,
) -> tuple[UserPydantic, WorkspacePydantic]:
    """Create user with an owned workspace."""
    user = await db.users.create(make_user())
    workspace = await db.workspaces.create(make_workspace())
    await db.user_workspaces.create(make_user_workspace(user.id, workspace.id))
    return user, workspace


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
