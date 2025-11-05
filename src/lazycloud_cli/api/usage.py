from datetime import datetime

from lazycloud_cli.api.base import BaseAPI
from shared.responses.usage import WorkspaceUsageWithDeploymentsResponse


class UsageAPI(BaseAPI):
    """API client for workspace usage endpoints"""

    def __init__(self):
        super().__init__("workspaces")

    def get_workspace_usage_with_deployments(
        self,
        workspace_id: str,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> WorkspaceUsageWithDeploymentsResponse:
        """Get workspace usage with deployment breakdowns in a single request."""
        params = {}
        if start_date:
            # Format as ISO 8601 with 'Z' suffix to match frontend format
            iso_str = start_date.isoformat()
            if iso_str.endswith("+00:00"):
                iso_str = iso_str.replace("+00:00", "Z")
            params["start_date"] = iso_str

        if end_date:
            # Format as ISO 8601 with 'Z' suffix to match frontend format
            iso_str = end_date.isoformat()
            if iso_str.endswith("+00:00"):
                iso_str = iso_str.replace("+00:00", "Z")
            params["end_date"] = iso_str

        response = self._get(f"/{workspace_id}/usage/with-deployments", params=params)
        return WorkspaceUsageWithDeploymentsResponse.model_validate(response)
