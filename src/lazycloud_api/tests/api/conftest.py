"""API test fixtures with AsyncClient and dependency overrides."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from lazycloud_api.api.dependencies import (
    check_workspace_limit,
    get_user_product_features,
)
from lazycloud_api.api.security import get_current_active_user, require_admin
from lazycloud_api.api.v1 import (
    api_keys_router,
    cli_version_router,
    deployments_router,
    diff_router,
    health_router,
    invitations_router,
    tasks_router,
    users_router,
    workspaces_router,
)
from lazycloud_api.billing.product_details.features import (
    BaseFeatures,
    DeploymentFeature,
    WorkspaceFeature,
)
from lazycloud_api.config import app_config
from lazycloud_api.database import Database, _create_database, get_db
from lazycloud_api.database.session import session_manager
from lazycloud_api.database.users import UserPydantic, UserRole
from lazycloud_api.services import (
    get_depot_service,
    get_ecr_auth_service,
    get_invitation_service,
    get_subscription_service,
    get_usage_service,
)
from lazycloud_api.tests.fixtures.database import make_user, requires_db

pytestmark = [pytest.mark.asyncio, requires_db]


def make_test_features() -> BaseFeatures:
    """Create mock subscription features for testing."""
    return BaseFeatures(
        workspace=WorkspaceFeature(limit=10, deployment_limit=5),
        deployment=DeploymentFeature(
            service_limit=10,
            volume_limit=5,
            network_limit=3,
            max_replicas_per_service=10,
        ),
        domain_limit=3,
    )


# Create test app once (stateless, can be reused)
_test_app = None


def get_test_app():
    """Get or create the test FastAPI app."""
    global _test_app
    if _test_app is None:
        from fastapi import APIRouter, FastAPI

        _test_app = FastAPI(title="LazyCloud API Test")

        # Versioned routes
        versioned = APIRouter(prefix=app_config.API_VERSION)
        versioned.include_router(users_router)
        versioned.include_router(api_keys_router)
        versioned.include_router(tasks_router)
        versioned.include_router(deployments_router)
        versioned.include_router(workspaces_router)
        versioned.include_router(invitations_router)
        versioned.include_router(diff_router)
        _test_app.include_router(versioned)

        # Non-versioned routes
        _test_app.include_router(health_router)
        _test_app.include_router(cli_version_router)

    return _test_app


@pytest.fixture
async def api_db_session() -> AsyncSession:
    """Provide a transactional session that rolls back after each test."""
    await session_manager.reset()
    session_manager._ensure_initialized()
    engine = session_manager.engine

    conn = await engine.connect()
    try:
        trans = await conn.begin()
        try:
            session = AsyncSession(bind=conn, expire_on_commit=False)
            try:
                yield session
            finally:
                await session.close()
        finally:
            await trans.rollback()
    finally:
        await conn.close()
        await session_manager.reset()


@pytest.fixture
def api_db(api_db_session: AsyncSession) -> Database:
    """Get Database instance bound to the test session."""
    return _create_database(api_db_session)


@pytest.fixture
async def api_user(api_db: Database) -> UserPydantic:
    """Create a test user for API tests."""
    return await api_db.users.create(make_user())


class MockTaskFuture:
    """Mock Prefect task future with task_run_id."""

    def __init__(self, task_run_id: str | None = None):
        self.task_run_id = task_run_id or str(uuid.uuid4())


def make_mock_usage_service() -> AsyncMock:
    """Create mock UsageService."""
    from datetime import datetime, timezone

    mock = AsyncMock()
    now = datetime.now(timezone.utc)
    period = {"start": now, "end": now}

    mock.get_aggregated_usage_with_summaries = AsyncMock(
        return_value={
            "period": period,
            "usage": {
                "cpu_core_hours": 0.0,
                "memory_gb_hours": 0.0,
                "standard_gb_hours": 0.0,
                "shared_gb_hours": 0.0,
                "build_minutes": 0.0,
                "public_endpoint_hours": 0.0,
            },
            "workspace_count": 0,
            "record_count": 0,
            "workspaces": [],
        }
    )
    mock.get_aggregated_daily_usage = AsyncMock(
        return_value={
            "period": period,
            "daily_usage": [],
            "workspace_count": 0,
        }
    )
    mock.get_deployment_cost_breakdown = AsyncMock(
        return_value={
            "workspace_id": "test",
            "period": period,
            "meter_breakdown": {
                "cpu_cost": 0.0,
                "memory_cost": 0.0,
                "standard_cost": 0.0,
                "shared_cost": 0.0,
                "build_cost": 0.0,
                "endpoint_cost": 0.0,
                "total_cost": 0.0,
            },
            "service_breakdown": [],
            "volume_breakdown": [],
            "is_estimated": True,
        }
    )
    return mock


def make_mock_subscription_service() -> AsyncMock:
    """Create mock SubscriptionService."""
    mock = AsyncMock()
    mock.get_user_features = AsyncMock(return_value=make_test_features())
    mock.check_workspace_limit = AsyncMock(return_value=None)
    mock.check_deployment_limit = AsyncMock(return_value=None)
    mock.validate_workspace_for_owner = AsyncMock(return_value=None)
    return mock


def make_mock_invitation_service() -> AsyncMock:
    """Create mock InvitationService."""
    mock = AsyncMock()
    mock.create_or_resend_invitation = AsyncMock(return_value="mock-token-123")
    mock.accept_invitation = AsyncMock(return_value=("workspace-id", "OWNER"))
    return mock


def make_mock_depot_service() -> AsyncMock:
    """Create mock DepotService."""
    mock = AsyncMock()
    mock.get_build_token = AsyncMock(return_value={"token": "mock-depot-token"})
    return mock


def make_mock_ecr_auth_service() -> AsyncMock:
    """Create mock ECRAuthService."""
    mock = AsyncMock()
    mock.get_upload_intent = AsyncMock(
        return_value={
            "registry": "123456789012.dkr.ecr.us-east-1.amazonaws.com",
            "username": "AWS",
            "password": "mock-password",
        }
    )
    mock.check_images_exist = AsyncMock(return_value={"exists": True})
    return mock


@pytest.fixture
async def api_admin_user(api_db: Database) -> UserPydantic:
    """Create an admin user for API key tests."""
    user = make_user("admin")
    user.role = UserRole.ADMIN
    return await api_db.users.create(user)


@pytest.fixture
def mock_prefect_tasks():
    """Mock Prefect tasks to return task futures."""
    from unittest.mock import patch

    mock_deploy = MagicMock()
    mock_deploy.delay = MagicMock(return_value=MockTaskFuture())

    mock_destroy = MagicMock()
    mock_destroy.delay = MagicMock(return_value=MockTaskFuture())

    mock_rollback = MagicMock()
    mock_rollback.delay = MagicMock(return_value=MockTaskFuture())

    mock_restart_service = MagicMock()
    mock_restart_service.delay = MagicMock(return_value=MockTaskFuture())

    mock_restart_all = MagicMock()
    mock_restart_all.delay = MagicMock(return_value=MockTaskFuture())

    mock_delete_instance = MagicMock()
    mock_delete_instance.delay = MagicMock(return_value=MockTaskFuture())

    with (
        patch("lazycloud_api.api.v1.deployments.root.deploy_compose_task", mock_deploy),
        patch(
            "lazycloud_api.api.v1.deployments.root.destroy_compose_task", mock_destroy
        ),
        patch(
            "lazycloud_api.api.v1.deployments.root.rollback_compose_task", mock_rollback
        ),
        patch(
            "lazycloud_api.api.v1.deployments.services.restart_service_task",
            mock_restart_service,
        ),
        patch(
            "lazycloud_api.api.v1.deployments.services.restart_all_services_task",
            mock_restart_all,
        ),
        patch(
            "lazycloud_api.api.v1.deployments.instances.delete_instance_task",
            mock_delete_instance,
        ),
    ):
        yield {
            "deploy": mock_deploy,
            "destroy": mock_destroy,
            "rollback": mock_rollback,
            "restart_service": mock_restart_service,
            "restart_all": mock_restart_all,
            "delete_instance": mock_delete_instance,
        }


@pytest.fixture
async def client(api_db: Database, api_user: UserPydantic) -> AsyncClient:
    """Async HTTP client with mocked auth and database dependencies."""
    app = get_test_app()

    # Override dependencies
    app.dependency_overrides[get_db] = lambda: api_db
    app.dependency_overrides[get_current_active_user] = lambda: api_user
    app.dependency_overrides[get_user_product_features] = make_test_features
    app.dependency_overrides[check_workspace_limit] = lambda: None
    app.dependency_overrides[get_usage_service] = make_mock_usage_service
    app.dependency_overrides[get_subscription_service] = make_mock_subscription_service
    app.dependency_overrides[get_invitation_service] = make_mock_invitation_service
    app.dependency_overrides[get_depot_service] = make_mock_depot_service
    app.dependency_overrides[get_ecr_auth_service] = make_mock_ecr_auth_service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


@pytest.fixture
async def admin_client(api_db: Database, api_admin_user: UserPydantic) -> AsyncClient:
    """Async HTTP client with admin user for API key tests."""

    app = get_test_app()

    app.dependency_overrides[get_db] = lambda: api_db
    app.dependency_overrides[get_current_active_user] = lambda: api_admin_user
    app.dependency_overrides[require_admin] = lambda: api_admin_user
    app.dependency_overrides[get_user_product_features] = make_test_features
    app.dependency_overrides[check_workspace_limit] = lambda: None

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()
