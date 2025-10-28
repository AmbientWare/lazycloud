from datetime import datetime

from lazycloud_cli.api.base import BaseAPI
from shared.responses.usage import DailyUsageResponse, WorkspaceUsageResponse


class UsageAPI(BaseAPI):
    """API client for workspace usage endpoints"""

    def __init__(self):
        super().__init__("workspaces")

    def get_usage(
        self,
        workspace_id: str,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        deployment_id: str | None = None,
    ) -> WorkspaceUsageResponse:
        """Get workspace usage. When deployment_id provided, includes service and volume breakdown."""
        params = {}
        if start_date:
            params["start_date"] = start_date.isoformat()
        if end_date:
            params["end_date"] = end_date.isoformat()
        if deployment_id:
            params["deployment_id"] = deployment_id

        response = self._get(f"/{workspace_id}/usage", params=params)
        return WorkspaceUsageResponse.model_validate(response)

    def get_daily_usage(
        self,
        workspace_id: str,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> DailyUsageResponse:
        """Get daily aggregated usage for sparkline visualization."""
        params = {}
        if start_date:
            params["start_date"] = start_date.isoformat()
        if end_date:
            params["end_date"] = end_date.isoformat()

        response = self._get(f"/{workspace_id}/usage/daily", params=params)
        return DailyUsageResponse.model_validate(response)
