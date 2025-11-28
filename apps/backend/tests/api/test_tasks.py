"""Tests for tasks API routes."""

import uuid
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from models.statuses import TaskStatus

from tests.fixtures.database import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]


class TestGetTaskStatus:
    """Tests for GET /v1/tasks/{task_id}."""

    async def test_get_task_status_success(self, client: AsyncClient):
        """Get task status returns status information."""
        task_id = uuid.uuid4()

        with patch(
            "backend.api.v1.tasks.get_task_result",
            return_value=(TaskStatus.COMPLETED, "Task completed"),
        ):
            response = await client.get(f"/v1/tasks/{task_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["task_id"] == str(task_id)
        assert data["status"] == TaskStatus.COMPLETED.value

    async def test_get_task_status_not_found(self, client: AsyncClient):
        """Non-existent task returns 404."""
        task_id = uuid.uuid4()

        with patch(
            "backend.api.v1.tasks.get_task_result",
            side_effect=Exception("Task not found"),
        ):
            response = await client.get(f"/v1/tasks/{task_id}")

        assert response.status_code == 404
