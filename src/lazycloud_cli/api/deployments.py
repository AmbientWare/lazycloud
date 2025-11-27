from datetime import UTC
from typing import Any, Callable

from lazycloud_cli.api.base import BaseAPI
from lazycloud_cli.api.base_sse import SSEClient
from lazycloud_cli.api.tasks import TasksAPI
from lazycloud_cli.config import config
from shared.requests.deployments import (
    DeploymentCreateRequest,
    DeploymentRunRequest,
    RollbackRequest,
)
from shared.responses.deployments import (
    DeploymentHistoryResponse,
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
        workspace_id: str,
        name: str | None = None,
    ) -> DeploymentResponse:
        """Create or update a deployment record (does not trigger deployment)."""
        request = DeploymentCreateRequest(
            compose_yaml=compose_yaml,
            workspace_id=workspace_id,
            name=name,
        )

        response_data = self._post("", json=request.model_dump())
        return DeploymentResponse(**response_data)

    def deploy_deployment(
        self,
        deployment_id: str,
        compose_yaml: str | None = None,
        secrets: bool = False,
        service_names: list[str] | None = None,
    ) -> DeploymentTaskStatusResponse:
        """Trigger deployment of a deployment record."""
        request = DeploymentRunRequest(
            compose_yaml=compose_yaml,
            secrets=secrets,
            service_names=service_names,
        )

        response_data = self._post(
            path=f"/{deployment_id}/deploy", json=request.model_dump()
        )
        return DeploymentTaskStatusResponse(**response_data)

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
        final_status = await self._tasks.stream_task_status(delete_response.task_id)

        # Return updated response with final status
        return DeploymentTaskStatusResponse(
            task_id=delete_response.task_id,
            deployment_id=delete_response.deployment_id,
            status=final_status.status,
            message=final_status.message,
        )

    def get_deployment(
        self,
        deployment_id: str | None = None,
        name: str | None = None,
        workspace_id: str | None = None,
    ) -> DeploymentResponse | None:
        """Get a specific deployment by ID."""
        # Use provided workspace_id or fall back to active workspace
        ws_id = workspace_id or config.active_workspace_id

        if deployment_id:
            response = self._get(
                path=f"?workspace_id={ws_id}&deployment_id={deployment_id}"
            )
        elif name:
            response = self._get(path=f"?workspace_id={ws_id}&name={name}")
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
        workspace_id: str | None = None,
        status: str | None = None,
        limit: int = 10,
        namespace: str | None = None,
    ) -> DeploymentListResponse:
        """List compose deployments."""
        # Use provided workspace_id or fall back to active workspace
        ws_id = workspace_id or config.active_workspace_id

        params: dict[str, Any] = {"limit": limit, "workspace_id": ws_id}
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

    def get_deployment_history(self, deployment_id: str) -> DeploymentHistoryResponse:
        """Get deployment revision history."""
        response = self._get(path=f"/{deployment_id}/history")
        return DeploymentHistoryResponse(**response)

    async def rollback_deployment(
        self, deployment_id: str, revision: int
    ) -> DeploymentTaskStatusResponse:
        """Rollback a deployment to a previous revision."""
        request = RollbackRequest(revision=revision)
        response_data = self._post(
            path=f"/{deployment_id}/rollback", json=request.model_dump()
        )
        rollback_response = DeploymentTaskStatusResponse(**response_data)

        final_status = await self._tasks.stream_task_status(rollback_response.task_id)

        return DeploymentTaskStatusResponse(
            task_id=rollback_response.task_id,
            deployment_id=rollback_response.deployment_id,
            status=final_status.status,
            message=final_status.message,
        )

    async def stream_deploy_progress(
        self,
        deployment_id: str,
        on_progress: Callable[[dict], None],
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        """Stream deployment progress with per-service status updates."""
        sse_client = SSEClient()

        def handle_event(event_type: str, data: dict) -> None:
            on_progress(data)

        await sse_client.stream(
            path=f"/deployments/{deployment_id}/status/deploy/stream",
            on_event=handle_event,
            on_error=on_error,
            max_retries=3,
        )
