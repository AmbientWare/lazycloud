"""Core test fixtures - users, workspaces, deployments."""

import uuid
from datetime import UTC, datetime

import pytest
from backend.database.models import (
    ComposeDeploymentInDb,
    SecretInDb,
    SubscriptionState,
    UserInDb,
    UserRole,
    UserStatus,
    UserWorkspaceInDb,
    UserWorkspaceStatus,
    WorkspaceInDb,
    WorkspaceRole,
    WorkspaceStatus,
)
from models.deployments import DeploymentStates
from models.secrets import SecretSource, SecretState

TEST_USER_ID = str(uuid.uuid4())
TEST_WORKSPACE_ID = str(uuid.uuid4())
TEST_DEPLOYMENT_ID = str(uuid.uuid4())


@pytest.fixture
def test_user() -> UserInDb:
    """Create a test user."""
    return UserInDb(
        id=TEST_USER_ID,
        name="Test User",
        email="test@example.com",
        workos_id="workos_test_123",
        role=UserRole.USER,
        status=UserStatus.ACTIVE,
        subscription_state=SubscriptionState.WITHIN_LIMITS,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.fixture
def admin_user() -> UserInDb:
    """Create an admin test user."""
    return UserInDb(
        id=str(uuid.uuid4() or ""),
        name="Admin User",
        email="admin@example.com",
        workos_id="workos_admin_123",
        role=UserRole.ADMIN,
        status=UserStatus.ACTIVE,
        subscription_state=SubscriptionState.WITHIN_LIMITS,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.fixture
def member_user() -> UserInDb:
    """Create a regular member user."""
    return UserInDb(
        id=str(uuid.uuid4() or ""),
        name="Member User",
        email="member@example.com",
        workos_id="workos_member_123",
        role=UserRole.USER,
        status=UserStatus.ACTIVE,
        subscription_state=SubscriptionState.WITHIN_LIMITS,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.fixture
def inactive_user() -> UserInDb:
    """Create an inactive user."""
    return UserInDb(
        id=str(uuid.uuid4() or ""),
        name="Inactive User",
        email="inactive@example.com",
        workos_id="workos_inactive_123",
        role=UserRole.USER,
        status=UserStatus.INACTIVE,
        subscription_state=SubscriptionState.WITHIN_LIMITS,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.fixture
def test_workspace() -> WorkspaceInDb:
    """Create a test workspace."""
    return WorkspaceInDb(
        id=TEST_WORKSPACE_ID,
        name="test-workspace",
        is_personal=False,
        status=WorkspaceStatus.ACTIVE,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.fixture
def test_membership(
    test_user: UserInDb, test_workspace: WorkspaceInDb
) -> UserWorkspaceInDb:
    """Create a test workspace membership with owner role."""
    return UserWorkspaceInDb(
        id=str(uuid.uuid4() or ""),
        user_id=str(test_user.id),
        workspace_id=str(test_workspace.id),
        role=WorkspaceRole.OWNER,
        status=UserWorkspaceStatus.ACTIVE,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.fixture
def admin_membership(
    admin_user: UserInDb, test_workspace: WorkspaceInDb
) -> UserWorkspaceInDb:
    """Create an admin workspace membership."""
    return UserWorkspaceInDb(
        id=str(uuid.uuid4() or ""),
        user_id=str(admin_user.id),
        workspace_id=str(test_workspace.id),
        role=WorkspaceRole.ADMIN,
        status=UserWorkspaceStatus.ACTIVE,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.fixture
def member_membership(
    member_user: UserInDb, test_workspace: WorkspaceInDb
) -> UserWorkspaceInDb:
    """Create a member workspace membership."""
    return UserWorkspaceInDb(
        id=str(uuid.uuid4() or ""),
        user_id=str(member_user.id),
        workspace_id=str(test_workspace.id),
        role=WorkspaceRole.MEMBER,
        status=UserWorkspaceStatus.ACTIVE,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.fixture
def test_deployment(test_workspace: WorkspaceInDb) -> ComposeDeploymentInDb:
    """Create a test deployment."""
    return ComposeDeploymentInDb(
        id=TEST_DEPLOYMENT_ID,
        name="test-deployment",
        workspace_id=str(test_workspace.id),
        namespace=f"lc-{test_workspace.id}",
        compose_yaml="version: '3.8'\nservices:\n  web:\n    image: nginx:latest",
        state=DeploymentStates.DEPLOYED,
        status_message="Deployed successfully",
        deployed_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        depot_project_id="test-depot-project",
        cluster_id="ash-1",
    )


@pytest.fixture
def test_secret(test_deployment: ComposeDeploymentInDb) -> SecretInDb:
    """Create a test secret."""
    return SecretInDb(
        id=str(uuid.uuid4() or ""),
        deployment_id=str(test_deployment.id),
        key="TEST_SECRET",
        value="secret_value",
        source=SecretSource.USER,
        state=SecretState.DEPLOYED,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
