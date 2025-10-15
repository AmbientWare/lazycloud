from datetime import UTC
from typing import Any

from lazycloud_cli.api.base import BaseAPI
from lazycloud_cli.api.tasks import TasksAPI
from shared.requests.deployments import DeploymentCreateRequest
from shared.responses.deployments import (
    DeploymentListResponse,
    DeploymentResponse,
    DeploymentStatusResponse,
)
from shared.responses.tasks import DeploymentTaskStatusResponse, TaskStatusResponse


class DeploymentsAPI(BaseAPI):
    def __init__(self):
        super().__init__("deployments")
        self._tasks = TasksAPI()

    def create_deployment(
        self,
        compose_yaml: str,
        name: str | None = None,
        secrets: bool = False,
    ) -> DeploymentTaskStatusResponse:
        """Create a deployment and return the task response."""

        request = DeploymentCreateRequest(
            compose_yaml=compose_yaml, name=name, secrets=secrets
        )

        # Make the initial request
        response_data = self._post("", json=request.model_dump())
        create_response = DeploymentTaskStatusResponse(**response_data)

        return create_response

    async def wait_for_deployment(self, task_id: str) -> TaskStatusResponse:
        """Wait for deployment task to complete via streaming."""
        return await self._tasks.stream_task_status(task_id)

    async def delete_deployment(
        self, deployment_id: str
    ) -> DeploymentTaskStatusResponse:
        """Delete a deployment with streaming task updates."""
        response_data = self._delete(path=f"/{deployment_id}")
        delete_response = DeploymentTaskStatusResponse(**response_data)

        # Stream task completion (no polling!)
        await self._tasks.wait_for_task_completion(delete_response.task_id)

        return delete_response

    def get_deployment(
        self, deployment_id: str | None = None, name: str | None = None
    ) -> DeploymentResponse | None:
        """Get a specific deployment by ID."""
        if deployment_id:
            response = self._get(path=f"?deployment_id={deployment_id}")
        elif name:
            response = self._get(path=f"?name={name}")
        else:
            raise ValueError("Either deployment_id or name must be provided")

        # deployments length is 0, return None
        if len(response.get("deployments", [])) == 0:
            return None
        elif len(response.get("deployments", [])) == 1:
            return DeploymentResponse(**response.get("deployments", [])[0])
        else:
            raise ValueError("Multiple deployments found")

    def list_deployments(
        self,
        status: str | None = None,
        limit: int = 10,
        namespace: str | None = None,
    ) -> DeploymentListResponse:
        """List compose deployments."""
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        if namespace:
            params["namespace"] = namespace

        response = self._get(params=params)
        return DeploymentListResponse(**response)

    def get_deployment_status(self, deployment_id: str) -> DeploymentStatusResponse:
        """Get resource status for a deployment."""
        response = self._get(path=f"/{deployment_id}/status")
        status = DeploymentStatusResponse(**response)
        status.status.last_checked = status.status.last_checked.replace(
            tzinfo=UTC
        ).astimezone()
        return status
