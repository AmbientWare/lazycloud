"""
API client for compose operations.
"""

from typing import Any

from lazycloud_cli.api.base import BaseAPI
from lazycloud_cli.api.tasks import tasks_api
from shared.models.tasks import DeploymentTaskStatusResponse
from shared.responses.deployments import (
    DeploymentListResponse,
    DeploymentResponse,
    DeploymentStatusResponse,
    DiffResponse,
    RestartResponse,
)


class DeploymentsAPI(BaseAPI):
    def __init__(self):
        super().__init__("deployments")

    def create_deployment(
        self,
        compose_yaml: str,
        name: str | None = None,
        secrets: bool = False,
    ) -> DeploymentTaskStatusResponse:
        """Create a deployment and return the task response."""
        json_data = {
            "compose_yaml": compose_yaml,
        }
        if name:
            json_data["name"] = name
        if secrets:
            json_data["secrets"] = True

        # Make the initial request
        response_data = self._post("deployments", json=json_data)

        # Return the task response
        return DeploymentTaskStatusResponse(**response_data)

    def delete_deployment(self, deployment_id: str) -> DeploymentTaskStatusResponse:
        """Delete a deployment."""
        response_data = self._delete(f"deployments/{deployment_id}")
        delete_response = DeploymentTaskStatusResponse(**response_data)

        #  wait for task to complete
        final_task_response = tasks_api.wait_for_task_completion(
            delete_response.task_id
        )

        return final_task_response

    def get_deployment(
        self, deployment_id: str | None = None, name: str | None = None
    ) -> DeploymentResponse | None:
        """Get a specific deployment by ID."""
        if deployment_id:
            response = self._get(f"deployments?deployment_id={deployment_id}")
        elif name:
            response = self._get(f"deployments?name={name}")
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

        response = self._get("deployments", params=params)
        return DeploymentListResponse(**response)

    def diff_deployment(
        self,
        deployment_id: str,
        compose_yaml: str,
        deployment_name: str | None = None,
        env_keys: list[str] | None = None,
    ) -> DiffResponse:
        """Get diff between current deployment and new compose file.

        For new deployments, use deployment_id='new' and provide deployment_name.
        """
        request_data: dict[str, Any] = {
            "compose_yaml": compose_yaml,
        }
        if deployment_name:
            request_data["deployment_name"] = deployment_name
        if env_keys:
            request_data["env_keys"] = env_keys

        response = self._post(f"deployments/{deployment_id}/diff", json=request_data)
        return DiffResponse(**response)

    def get_deployment_status(self, deployment_id: str) -> DeploymentStatusResponse:
        """Get resource status for a deployment."""
        response = self._get(f"deployments/{deployment_id}/status")
        return DeploymentStatusResponse(**response)

    def restart_service(self, deployment_id: str, service_name: str) -> RestartResponse:
        """Restart a specific service in a deployment."""
        response_data = self._post(
            f"deployments/{deployment_id}/restart/{service_name}"
        )
        return RestartResponse(**response_data)

    def restart_all_services(self, deployment_id: str) -> RestartResponse:
        """Restart all services in a deployment."""
        response_data = self._post(f"deployments/{deployment_id}/restart")
        return RestartResponse(**response_data)


# Create instance for other modules to import
deployments_api = DeploymentsAPI()
