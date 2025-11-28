"""Tests for deployment usage API routes."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from backend.database import Database
from backend.database.users import UserPydantic
from backend.services import (
    get_depot_service,
    get_polar_service,
    get_usage_service,
)
from backend.services.polar.cost_breakdown import (
    MeterCostBreakdown,
    WorkspaceCostBreakdown,
)
from httpx import AsyncClient
from models.billing import UsageCollectionConfig
from models.workspaces import WorkspaceRole
from responses.usage import UsageMetrics

from tests.api.conftest import get_test_app
from tests.fixtures.database import (
    make_deployment,
    make_usage_record,
    make_user_workspace,
    make_workspace,
    requires_db,
)

pytestmark = [pytest.mark.asyncio, requires_db]


class TestGetDeploymentCostBreakdown:
    """Tests for GET /v1/deployments/{id}/usage/breakdown."""

    async def test_get_cost_breakdown(
        self,
        client: AsyncClient,
        api_db: Database,
        api_user: UserPydantic,
    ):
        """Get cost breakdown for deployment."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        deployment = await api_db.compose_deployments.create(
            make_deployment(workspace.id, name="test-deploy")
        )

        now = datetime.now(timezone.utc)
        # Create usage record for current month (endpoint defaults to start of month)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        usage_record = make_usage_record(
            str(workspace.id),
            collection_start=month_start,
            collection_end=now,
        )
        # Set correct record type to match UsageCollectionConfig
        usage_record.record_type = UsageCollectionConfig.get_record_type().value
        await api_db.usage.create(usage_record)

        # Override dependencies in the app
        app = get_test_app()

        # Mock Polar service
        mock_polar_service = AsyncMock()
        mock_polar_service.enabled = True
        mock_cost_breakdown = WorkspaceCostBreakdown(
            meter_breakdown=MeterCostBreakdown(
                cpu_cost=0.0,
                memory_cost=0.0,
                standard_cost=0.0,
                shared_cost=0.0,
                build_cost=0.0,
                endpoint_cost=0.0,
                total_cost=0.0,
            ),
            service_breakdown=[],
            volume_breakdown=[],
        )
        mock_polar_service.cost_breakdown.calculate_workspace_costs = AsyncMock(
            return_value=mock_cost_breakdown
        )

        # Mock Depot service
        mock_depot_service = AsyncMock()
        mock_depot_service.get_deployment_build_minutes = AsyncMock(return_value=0.0)

        # Mock Usage service
        mock_usage_service = MagicMock()
        mock_usage_service.aggregate_deployment_usage_from_records = MagicMock(
            return_value=(
                UsageMetrics(
                    cpu_core_hours=0.0,
                    memory_gb_hours=0.0,
                    standard_gb_hours=0.0,
                    shared_gb_hours=0.0,
                    build_minutes=0.0,
                    public_endpoint_hours=0.0,
                ),
                [],
                [],
            )
        )

        app.dependency_overrides[get_polar_service] = lambda: mock_polar_service
        app.dependency_overrides[get_depot_service] = lambda: mock_depot_service
        app.dependency_overrides[get_usage_service] = lambda: mock_usage_service

        response = await client.get(f"/v1/deployments/{deployment.id}/usage/breakdown")

        assert response.status_code == 200
        data = response.json()
        assert "meter_breakdown" in data
        # Clean up overrides
        app.dependency_overrides.clear()
