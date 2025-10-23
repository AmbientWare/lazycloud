from lazycloud_cli.api.base import BaseAPI
from shared.requests.deployments import DiffRequest
from shared.responses.deployments import DiffResponse


class DiffAPI(BaseAPI):
    def __init__(self):
        super().__init__("deployments")

    def get_deployment_diff(
        self,
        deployment_id: str,
        workspace_id: str,
        compose_yaml: str,
        deployment_name: str | None = None,
        env_keys: list[str] | None = None,
    ) -> DiffResponse:
        """Get diff between current deployment and new compose file."""
        request = DiffRequest(
            compose_yaml=compose_yaml,
            deployment_name=deployment_name,
            workspace_id=workspace_id,
            env_keys=env_keys,
        )

        # New REST structure: /deployments/{deployment_id}/diff
        response = self._post(f"/{deployment_id}/diff", json=request.model_dump())
        return DiffResponse(**response)
