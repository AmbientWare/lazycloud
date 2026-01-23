"""API test fixtures with AsyncClient and dependency overrides."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from backend.api.dependencies import get_user_product_features
from backend.api.security import (
    get_current_active_user,
    get_current_user,
    require_admin,
)
from backend.api.v1 import (
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
from backend.billing.product_details.features import BaseFeatures
from backend.config import app_config
from backend.database import Database, _create_database, get_db
from backend.database.session import session_manager
from backend.database.users import UserPydantic, UserRole
from backend.services import (
    get_depot_service,
    get_invitation_service,
    get_subscription_service,
    get_usage_service,
)
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.fixtures.database import make_user, requires_db

pytestmark = [pytest.mark.asyncio, requires_db]


def make_test_features() -> BaseFeatures:
    """Create mock subscription features for testing."""
    return BaseFeatures(
        deployment_limit=10,
        max_team_members=10,
        max_cpu_per_service=8.0,
        max_memory_per_service=16,
        max_replicas_per_service=10,
        custom_domains_enabled=True,
        support_level="email",
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


def make_mock_job_key() -> str:
    """Generate a mock SAQ job key."""
    return str(uuid.uuid4())


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


@pytest.fixture
async def api_admin_user(api_db: Database) -> UserPydantic:
    """Create an admin user for API key tests."""
    user = make_user("admin")
    user.role = UserRole.ADMIN
    return await api_db.users.create(user)


@pytest.fixture
def mock_saq_tasks():
    """Mock SAQ client functions to return job keys."""
    mock_deploy = AsyncMock(return_value=make_mock_job_key())
    mock_destroy = AsyncMock(return_value=make_mock_job_key())
    mock_rollback = AsyncMock(return_value=make_mock_job_key())
    mock_restart_service = AsyncMock(return_value=make_mock_job_key())
    mock_restart_all = AsyncMock(return_value=make_mock_job_key())
    mock_delete_instance = AsyncMock(return_value=make_mock_job_key())

    with (
        patch("backend.api.v1.deployments.root.run_deploy_compose", mock_deploy),
        patch("backend.api.v1.deployments.root.run_destroy_compose", mock_destroy),
        patch("backend.api.v1.deployments.root.run_rollback_compose", mock_rollback),
        patch(
            "backend.api.v1.deployments.services.run_restart_service",
            mock_restart_service,
        ),
        patch(
            "backend.api.v1.deployments.services.run_restart_all_services",
            mock_restart_all,
        ),
        patch(
            "backend.api.v1.deployments.instances.run_delete_instance",
            mock_delete_instance,
        ),
        patch(
            "backend.api.v1.workspaces.root.run_destroy_compose",
            mock_destroy,
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
    app.dependency_overrides[get_current_user] = lambda: api_user
    app.dependency_overrides[get_current_active_user] = lambda: api_user
    app.dependency_overrides[require_admin] = lambda: api_user
    app.dependency_overrides[get_user_product_features] = make_test_features
    app.dependency_overrides[get_usage_service] = make_mock_usage_service
    app.dependency_overrides[get_subscription_service] = make_mock_subscription_service
    app.dependency_overrides[get_invitation_service] = make_mock_invitation_service
    app.dependency_overrides[get_depot_service] = make_mock_depot_service

    # Mock quota capacity checks to bypass limits in tests
    with (
        patch(
            "backend.services.compose.validation.verify_quota_capacity",
            new_callable=AsyncMock,
        ) as mock_verify_quota,
    ):
        mock_verify_quota.return_value = None

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    app.dependency_overrides.clear()


@pytest.fixture
async def admin_client(api_db: Database, api_admin_user: UserPydantic) -> AsyncClient:
    """Async HTTP client with admin user for API key tests."""

    app = get_test_app()

    app.dependency_overrides[get_db] = lambda: api_db
    app.dependency_overrides[get_current_user] = lambda: api_admin_user
    app.dependency_overrides[get_current_active_user] = lambda: api_admin_user
    app.dependency_overrides[require_admin] = lambda: api_admin_user
    app.dependency_overrides[get_user_product_features] = make_test_features

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()
