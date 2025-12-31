"""Core test fixtures - users, workspaces, deployments."""

import uuid
from datetime import UTC, datetime

import pytest
from backend.database.compose import ComposeDeploymentPydantic
from backend.database.secrets import SecretPydantic
from backend.database.user_workspaces import UserWorkspacePydantic
from backend.database.users import (
    SubscriptionState,
    UserPydantic,
    UserRole,
    UserStatus,
)
from backend.database.workspaces import WorkspacePydantic, WorkspaceStatus
from models.deployments import DeploymentStates
from models.secrets import SecretSource, SecretState
from models.workspaces import UserWorkspaceStatus, WorkspaceRole

TEST_USER_ID = str(uuid.uuid4())
TEST_WORKSPACE_ID = str(uuid.uuid4())
TEST_DEPLOYMENT_ID = str(uuid.uuid4())


@pytest.fixture
def test_user() -> UserPydantic:
    """Create a test user."""
    return UserPydantic(
        id=uuid.UUID(TEST_USER_ID),
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
def admin_user() -> UserPydantic:
    """Create an admin test user."""
    return UserPydantic(
        id=uuid.uuid4(),
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
def member_user() -> UserPydantic:
    """Create a regular member user."""
    return UserPydantic(
        id=uuid.uuid4(),
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
def inactive_user() -> UserPydantic:
    """Create an inactive user."""
    return UserPydantic(
        id=uuid.uuid4(),
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
def test_workspace() -> WorkspacePydantic:
    """Create a test workspace."""
    return WorkspacePydantic(
        id=uuid.UUID(TEST_WORKSPACE_ID),
        name="test-workspace",
        is_personal=False,
        status=WorkspaceStatus.ACTIVE,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.fixture
def test_membership(
    test_user: UserPydantic, test_workspace: WorkspacePydantic
) -> UserWorkspacePydantic:
    """Create a test workspace membership with owner role."""
    return UserWorkspacePydantic(
        user_id=str(test_user.id),
        workspace_id=str(test_workspace.id),
        role=WorkspaceRole.OWNER,
        status=UserWorkspaceStatus.ACTIVE,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.fixture
def admin_membership(
    admin_user: UserPydantic, test_workspace: WorkspacePydantic
) -> UserWorkspacePydantic:
    """Create an admin workspace membership."""
    return UserWorkspacePydantic(
        user_id=str(admin_user.id),
        workspace_id=str(test_workspace.id),
        role=WorkspaceRole.ADMIN,
        status=UserWorkspaceStatus.ACTIVE,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.fixture
def member_membership(
    member_user: UserPydantic, test_workspace: WorkspacePydantic
) -> UserWorkspacePydantic:
    """Create a member workspace membership."""
    return UserWorkspacePydantic(
        user_id=str(member_user.id),
        workspace_id=str(test_workspace.id),
        role=WorkspaceRole.MEMBER,
        status=UserWorkspaceStatus.ACTIVE,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.fixture
def test_deployment(test_workspace: WorkspacePydantic) -> ComposeDeploymentPydantic:
    """Create a test deployment."""
    return ComposeDeploymentPydantic(
        id=uuid.UUID(TEST_DEPLOYMENT_ID),
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
    )


@pytest.fixture
def test_secret(test_deployment: ComposeDeploymentPydantic) -> SecretPydantic:
    """Create a test secret."""
    return SecretPydantic(
        id=uuid.uuid4(),
        deployment_id=str(test_deployment.id),
        key="TEST_SECRET",
        value="secret_value",
        source=SecretSource.USER,
        state=SecretState.DEPLOYED,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
