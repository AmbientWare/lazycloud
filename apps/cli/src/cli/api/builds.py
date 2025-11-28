from responses.builds import DepotTokenResponse

from cli.api.base import BaseAPI
from cli.config import config


class BuildsAPI(BaseAPI):
    """API client for remote build operations."""

    def __init__(self):
        super().__init__("workspaces")

    def get_depot_token(self, deployment_name: str) -> DepotTokenResponse:
        """Get Depot project token for remote builds."""
        workspace_id = config.active_workspace_id

        response_data = self._post(
            f"/{workspace_id}/builds/token?deployment_name={deployment_name}"
        )
        return DepotTokenResponse(**response_data)
