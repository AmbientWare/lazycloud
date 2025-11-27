"""Quota test fixtures - mocks get_db_context for both subscription_service and deployment utils."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

import lazycloud_api.prefect_app.deployment.utils as deployment_utils_module
import lazycloud_api.services.subscription_service as subscription_module
from lazycloud_api.database import Database
from lazycloud_api.services.polar import PolarService
from lazycloud_api.services.subscription_service import SubscriptionService
from lazycloud_api.tests.fixtures.database import make_features


@pytest.fixture(autouse=True)
def mock_db_context(db_session, db: Database):
    """Mock get_db_context in both subscription_service and deployment utils modules."""

    @asynccontextmanager
    async def mock_context():
        try:
            yield db
        finally:
            await db_session.flush()

    # Patch subscription_service module
    original_subscription = subscription_module.get_db_context
    subscription_module.get_db_context = mock_context

    # Patch deployment utils module
    original_deployment = deployment_utils_module.get_db_context
    deployment_utils_module.get_db_context = mock_context

    yield

    # Restore originals
    subscription_module.get_db_context = original_subscription
    deployment_utils_module.get_db_context = original_deployment


@pytest.fixture
def subscription_service() -> SubscriptionService:
    """Create SubscriptionService instance with disabled Polar."""
    polar_service = PolarService(access_token="", is_sandbox=True)
    return SubscriptionService(polar_service=polar_service)


@pytest.fixture
def mock_subscription_service(request):
    """Mock subscription service with configurable features for quota capacity tests."""
    features_params = getattr(request, "param", {})
    features = make_features(**features_params)

    mock_service = AsyncMock()
    mock_service.get_user_features = AsyncMock(return_value=features)

    original = deployment_utils_module.get_subscription_service

    def mock_get_subscription_service():
        return mock_service

    deployment_utils_module.get_subscription_service = mock_get_subscription_service
    yield mock_service
    deployment_utils_module.get_subscription_service = original
