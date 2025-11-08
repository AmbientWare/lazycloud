from datetime import datetime

from lazycloud_cli.api.base import BaseAPI
from lazycloud_cli.config import config
from shared.responses.usage import (
    AggregatedUsageResponse,
    WorkspaceCostBreakdownResponse,
)


class UsageAPI(BaseAPI):
    """API client for workspace usage endpoints"""

    def __init__(self):
        super().__init__("workspaces")

    def get_aggregated_usage(
        self,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> AggregatedUsageResponse:
        """Get aggregated usage across all user's workspaces with workspace summaries."""
        params = {}
        if start_date:
            iso_str = start_date.isoformat()
            if iso_str.endswith("+00:00"):
                iso_str = iso_str.replace("+00:00", "Z")
            params["start_date"] = iso_str

        if end_date:
            iso_str = end_date.isoformat()
            if iso_str.endswith("+00:00"):
                iso_str = iso_str.replace("+00:00", "Z")
            params["end_date"] = iso_str

        response = self._get("/usage/all", params=params)
        return AggregatedUsageResponse.model_validate(response)

    def get_deployment_cost_breakdown(
        self,
        deployment_id: str,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> WorkspaceCostBreakdownResponse:
        """Get detailed cost breakdown for a specific deployment with service and volume details."""
        params = {}
        if start_date:
            iso_str = start_date.isoformat()
            if iso_str.endswith("+00:00"):
                iso_str = iso_str.replace("+00:00", "Z")
            params["start_date"] = iso_str

        if end_date:
            iso_str = end_date.isoformat()
            if iso_str.endswith("+00:00"):
                iso_str = iso_str.replace("+00:00", "Z")
            params["end_date"] = iso_str

        # Construct full URL for deployments endpoint
        base_url = f"{config.api_base_url}/{config.api_version}/deployments"
        url = f"{base_url}/{deployment_id}/usage/breakdown"
        response = self._make_request("GET", url, None, params)
        return WorkspaceCostBreakdownResponse.model_validate(response)
