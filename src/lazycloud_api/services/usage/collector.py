import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx

from lazycloud_api.config import app_config


@dataclass
class UsageRecord:
    """Represents a single usage record from Fly.io API."""

    value: float
    app: str
    labels: dict[str, Any]
    day: str


@dataclass
class UsageResponse:
    """Represents the response structure from Fly.io usage API."""

    data: list[UsageRecord]
    more: bool
    cursor: str | None = None


class UsageCollector:
    """Class to collect usage data from Fly.io's usage API."""

    def __init__(self, debug: bool = True):
        """Initialize the collector with organization and client."""
        self.client: httpx.AsyncClient | None = None
        self.org_slug: str = app_config.FLY_ORG_NAME
        self.base_url: str = (
            f"https://api.fly.io/api/v1/organizations/{self.org_slug}/usage"
        )
        self.debug: bool = debug

    async def __aenter__(self):
        """Async context manager entry."""
        self.client = httpx.AsyncClient()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        if self.client:
            await self.client.aclose()
            self.client = None

    def _get_headers(self) -> dict[str, str]:
        """Get headers for API requests."""
        return {"Authorization": f"Bearer {app_config.FLY_API_TOKEN}"}

    def _format_date(self, date: datetime) -> str:
        """Format date for API request (YYYY-MM-DD)."""
        return date.strftime("%Y-%m-%d")

    async def _fetch_usage_page(
        self,
        resource_kind: str,
        start_date: str,
        end_date: str,
        app: str | None = None,
        cursor: str | None = None,
    ) -> UsageResponse:
        """Fetch a single page of usage data."""
        if not self.client:
            raise ValueError("Client not initialized - use async context manager")

        params = {
            "start_date": start_date,
            "end_date": end_date,
        }

        if app:
            params["app"] = app

        if cursor:
            params["cursor"] = cursor

        url = f"{self.base_url}/{resource_kind}"

        response = await self.client.get(
            url,
            params=params,
            headers=self._get_headers(),
        )

        # Instead of just raising, let's capture the error details
        if response.status_code != 200:
            error_details = {
                "status_code": response.status_code,
                "url": str(response.url),
                "headers": dict(response.headers),
                "response_text": response.text,
            }

            # Try to parse JSON error if available
            try:
                error_json = response.json()
                error_details["response_json"] = error_json
            except Exception as e:
                print(f"Error parsing JSON: {e}")
                pass

            print("=== API ERROR DETAILS ===")
            print(f"Status Code: {error_details['status_code']}")
            print(f"URL: {error_details['url']}")
            print(f"Response Text: {error_details['response_text']}")
            if "response_json" in error_details:
                print(f"Response JSON: {error_details['response_json']}")
            print(f"Response Headers: {error_details['headers']}")
            print("=========================")

            # Still raise the exception, but now we have the details
            response.raise_for_status()

        data = response.json()

        # Debug: Print the actual API response
        if self.debug:
            print("=== DEBUG: API RESPONSE ===")
            print(f"URL: {response.url}")
            print(f"Status: {response.status_code}")
            print(f"Response JSON: {json.dumps(data, indent=2)}")
            print("=========================")

        # Parse response into structured format
        usage_records = [
            UsageRecord(
                value=float(record["value"]),
                app=record["app"],
                labels=record["labels"],
                day=record["day"],
            )
            for record in data.get("data", [])
        ]

        page_info = data.get("page", {})

        return UsageResponse(
            data=usage_records,
            more=page_info.get("more", False),
            cursor=page_info.get("cursor"),
        )

    async def _fetch_all_usage_pages(
        self,
        resource_kind: str,
        start_date: str,
        end_date: str,
        app: str | None = None,
    ) -> list[UsageRecord]:
        """Fetch all pages of usage data for a resource kind."""
        all_records = []
        cursor = None

        while True:
            response = await self._fetch_usage_page(
                resource_kind=resource_kind,
                start_date=start_date,
                end_date=end_date,
                app=app,
                cursor=cursor,
            )

            all_records.extend(response.data)

            if not response.more:
                break

            cursor = response.cursor

        return all_records

    async def get_machine_usage(
        self,
        start_date: datetime,
        end_date: datetime,
        app: str | None = None,
    ) -> list[UsageRecord]:
        """Get machine usage data between start and end dates."""
        start_str = self._format_date(start_date)
        end_str = self._format_date(end_date)

        return await self._fetch_all_usage_pages(
            resource_kind="machine",
            start_date=start_str,
            end_date=end_str,
            app=app,
        )

    async def get_volume_usage(
        self,
        start_date: datetime,
        end_date: datetime,
        app: str | None = None,
    ) -> list[UsageRecord]:
        """Get volume usage data between start and end dates."""
        start_str = self._format_date(start_date)
        end_str = self._format_date(end_date)

        return await self._fetch_all_usage_pages(
            resource_kind="volume",
            start_date=start_str,
            end_date=end_str,
            app=app,
        )

    async def get_daily_usage(
        self,
        target_date: datetime,
        app: str | None = None,
    ) -> tuple[list[UsageRecord], list[UsageRecord]]:
        """Get both machine and volume usage for a specific day."""
        # API requires end_date to be after start_date, so for a single day:
        # start_date = target_date, end_date = target_date + 1 day
        start_date = target_date
        end_date = target_date + timedelta(days=1)

        machine_usage = await self.get_machine_usage(
            start_date=start_date,
            end_date=end_date,
            app=app,
        )

        volume_usage = await self.get_volume_usage(
            start_date=start_date,
            end_date=end_date,
            app=app,
        )

        return machine_usage, volume_usage

    async def get_usage_for_period(
        self,
        start_date: datetime,
        end_date: datetime,
        app: str | None = None,
    ) -> tuple[list[UsageRecord], list[UsageRecord]]:
        """Get both machine and volume usage for a date range."""
        # Validate date range (max 31 days as per API limits)
        date_diff = (end_date - start_date).days
        if date_diff > 31:
            raise ValueError("Date range cannot exceed 31 days")

        machine_usage = await self.get_machine_usage(
            start_date=start_date,
            end_date=end_date,
            app=app,
        )

        volume_usage = await self.get_volume_usage(
            start_date=start_date,
            end_date=end_date,
            app=app,
        )

        return machine_usage, volume_usage

    def group_usage_by_app(
        self,
        machine_usage: list[UsageRecord],
        volume_usage: list[UsageRecord],
    ) -> dict[str, dict]:
        """Group usage records by app name."""
        grouped = {}

        # Process machine usage
        for record in machine_usage:
            app = record.app
            if app not in grouped:
                grouped[app] = {
                    "machine_usage": [],
                    "volume_usage": [],
                }

            grouped[app]["machine_usage"].append(
                {
                    "value": record.value,
                    "labels": record.labels,
                    "day": record.day,
                }
            )

        # Process volume usage
        for record in volume_usage:
            app = record.app
            if app not in grouped:
                grouped[app] = {
                    "machine_usage": [],
                    "volume_usage": [],
                }

            grouped[app]["volume_usage"].append(
                {
                    "value": record.value,
                    "labels": record.labels,
                    "day": record.day,
                }
            )

        return grouped


# Example usage function
async def main():
    """Example usage of the FlyUsageCollector."""
    async with UsageCollector() as collector:
        # Get usage for yesterday (since we can't query "today" yet)
        yesterday = datetime.now() - timedelta(days=1)

        print(f"=== Usage for {yesterday.strftime('%Y-%m-%d')} ===")

        machine_usage, volume_usage = await collector.get_daily_usage(yesterday)

        # Group by app
        grouped_usage = collector.group_usage_by_app(machine_usage, volume_usage)

        print(json.dumps(grouped_usage, indent=2))


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
