"""Tests for deployment usage API routes."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from backend.database import Database
from backend.database.models import UserInDb, WorkspaceRole
from backend.services import (
    get_polar_service,
    get_usage_service,
)
from backend.services.polar.cost_breakdown import (
    MeterCostBreakdown,
    WorkspaceCostBreakdown,
)
from httpx import AsyncClient
from models.usage import BreakdownType
from responses.usage import UsageMetrics

from tests.api.conftest import get_test_app
from tests.fixtures.database import (
    make_deployment,
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
        api_user: UserInDb,
    ):
        """Get cost breakdown for deployment."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        deployment = await api_db.compose_deployments.create(
            make_deployment(workspace.id, name="test-deploy")
        )

        # Create breakdown events for this deployment
        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        interval_end = month_start + timedelta(minutes=15)

        await api_db.usage.add_breakdown_event(
            workspace_id=workspace.id,
            interval_start=month_start,
            interval_end=interval_end,
            breakdown_type=BreakdownType.COMPUTE,
            resource_name="web-0",
            deployment_id=deployment.id,
            service_name="web",
            cpu_core_seconds=100.0,
            memory_gb_seconds=200.0,
        )

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

        # Mock Usage service with the new interface
        mock_usage_service = AsyncMock()
        mock_usage_service.get_deployment_breakdown = AsyncMock(
            return_value=(
                UsageMetrics(
                    cpu_core_hours=0.0,
                    memory_gb_hours=0.0,
                    build_minutes=0.0,
                    storage_gb_months=0.0,
                ),
                [],
                [],
            )
        )

        app.dependency_overrides[get_polar_service] = lambda: mock_polar_service
        app.dependency_overrides[get_usage_service] = lambda: mock_usage_service

        response = await client.get(f"/v1/deployments/{deployment.id}/usage/breakdown")

        assert response.status_code == 200
        data = response.json()
        # Verify response structure
        assert "meter_breakdown" in data
        assert "service_breakdown" in data
        assert "volume_breakdown" in data
        # Verify meter breakdown structure
        meter = data["meter_breakdown"]
        assert "cpu_cost" in meter
        assert "memory_cost" in meter
        assert "total_cost" in meter
        # Clean up overrides
        app.dependency_overrides.clear()

    async def test_get_cost_breakdown_not_found(self, client: AsyncClient):
        """Non-existent deployment returns 404."""
        response = await client.get(
            "/v1/deployments/00000000-0000-0000-0000-000000000000/usage/breakdown"
        )
        assert response.status_code == 404
