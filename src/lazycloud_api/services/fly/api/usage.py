from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from tenacity import retry, stop_after_attempt, wait_exponential

from lazycloud_api.services.fly.api.base import BaseFlyAPI
from lazycloud_api.config import app_config


@dataclass
class MachineRecord:
    """Represents a machine usage record with structured labels."""

    value: float  # seconds
    app: str
    day: str
    cpu_count: Optional[int] = None
    cpu_kind: Optional[str] = None
    memory_mb: Optional[int] = None
    memory_gb: Optional[float] = None
    region: Optional[str] = None

    def __post_init__(self):
        """Calculate memory_gb from memory_mb if available."""
        if self.memory_mb and not self.memory_gb:
            self.memory_gb = self.memory_mb / 1024

    @property
    def hours(self) -> float:
        """Get usage in hours."""
        return self.value / 3600

    def format_config(self) -> str:
        """Format machine configuration for display."""
        parts = []
        if self.cpu_count:
            parts.append(f"{self.cpu_count} CPU")
        if self.cpu_kind:
            parts.append(f"({self.cpu_kind})")
        if self.memory_gb:
            parts.append(f"{self.memory_gb:.1f}GB RAM")
        if self.region:
            parts.append(f"in {self.region}")

        return " ".join(parts) if parts else "Unknown config"


@dataclass
class VolumeRecord:
    """Represents a volume usage record with structured labels."""

    value: float  # GB-hours
    app: str
    day: str
    volume_type: Optional[str] = None
    region: Optional[str] = None


@dataclass
class UsageRecord:
    """Legacy usage record - kept for internal API parsing."""

    value: float
    app: str
    labels: Dict[str, Any]
    day: str


@dataclass
class UsageResponse:
    """Represents the response structure from Fly.io usage API."""

    data: List[UsageRecord]
    more: bool
    cursor: Optional[str] = None


@dataclass
class AppUsageSummary:
    """Simple summary of usage data for a single app."""

    app_name: str
    period_start: str
    period_end: str
    machine_records: List[MachineRecord]
    volume_records: List[VolumeRecord]


class UsageAPI(BaseFlyAPI):
    """API client for Fly.io usage data."""

    def __init__(self):
        super().__init__()
        # NOTE: this api is different from normal Machines API.
        self._base_url = (
            f"https://api.fly.io/api/v1/organizations/{app_config.FLY_ORG_NAME}/usage"
        )

    def _format_date(self, date: datetime) -> str:
        """Format date for API request (YYYY-MM-DD)."""
        return date.strftime("%Y-%m-%d")

    def _convert_to_machine_record(self, usage_record: UsageRecord) -> MachineRecord:
        """Convert a UsageRecord to a MachineRecord with structured labels."""
        labels = usage_record.labels
        return MachineRecord(
            value=usage_record.value,
            app=usage_record.app,
            day=usage_record.day,
            cpu_count=labels.get("cpu_count"),
            cpu_kind=labels.get("cpu_kind"),
            memory_mb=labels.get("memory_mb"),
            region=labels.get("region"),
        )

    def _convert_to_volume_record(self, usage_record: UsageRecord) -> VolumeRecord:
        """Convert a UsageRecord to a VolumeRecord with structured labels."""
        labels = usage_record.labels
        return VolumeRecord(
            value=usage_record.value,
            app=usage_record.app,
            day=usage_record.day,
            volume_type=labels.get("volume_type"),
            region=labels.get("region"),
        )

    @retry(
        stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=15)
    )
    async def _fetch_usage_page(
        self,
        resource_kind: str,
        start_date: str,
        end_date: str,
        app: Optional[str] = None,
        cursor: Optional[str] = None,
    ) -> UsageResponse:
        """Fetch a single page of usage data."""
        params = {
            "start_date": start_date,
            "end_date": end_date,
        }

        if app:
            params["app"] = app

        if cursor:
            params["cursor"] = cursor

        # Use httpx directly since this is a different base URL
        import httpx

        headers = {"Authorization": f"Bearer {app_config.FLY_API_TOKEN}"}

        async with httpx.AsyncClient() as client:
            url = f"{self._base_url}/{resource_kind}"
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            data = response.json()

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
        app: Optional[str] = None,
    ) -> List[UsageRecord]:
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

    async def get_usage_for_period(
        self,
        start_date: datetime,
        end_date: datetime,
        app: Optional[str] = None,
    ) -> List[AppUsageSummary]:
        """Get usage data for a date range.

        Args:
            start_date: Start date for usage data
            end_date: End date for usage data
            app: Optional app name to filter results

        Returns:
            List of AppUsageSummary objects, one per app with their usage records.
        """
        # Validate date range (max 31 days as per API limits)
        date_diff = (end_date - start_date).days
        if date_diff > 31:
            raise ValueError("Date range cannot exceed 31 days")

        # Fetch usage data
        machine_usage = await self._fetch_all_usage_pages(
            resource_kind="machine",
            start_date=self._format_date(start_date),
            end_date=self._format_date(end_date),
            app=app,
        )

        volume_usage = await self._fetch_all_usage_pages(
            resource_kind="volume",
            start_date=self._format_date(start_date),
            end_date=self._format_date(end_date),
            app=app,
        )

        # Group by app
        app_data = {}

        # Group machine usage by app and convert to MachineRecord
        for record in machine_usage:
            app_name = record.app
            if app_name not in app_data:
                app_data[app_name] = {
                    "machine_records": [],
                    "volume_records": [],
                }
            machine_record = self._convert_to_machine_record(record)
            app_data[app_name]["machine_records"].append(machine_record)

        # Group volume usage by app and convert to VolumeRecord
        for record in volume_usage:
            app_name = record.app
            if app_name not in app_data:
                app_data[app_name] = {
                    "machine_records": [],
                    "volume_records": [],
                }
            volume_record = self._convert_to_volume_record(record)
            app_data[app_name]["volume_records"].append(volume_record)

        # Create AppUsageSummary objects for each app
        summaries = []
        for app_name, data in app_data.items():
            summary = AppUsageSummary(
                app_name=app_name,
                period_start=self._format_date(start_date),
                period_end=self._format_date(end_date),
                machine_records=data["machine_records"],
                volume_records=data["volume_records"],
            )
            summaries.append(summary)

        return summaries

    async def get_monthly_usage(
        self,
        month: Optional[datetime] = None,
        app: Optional[str] = None,
    ) -> List[AppUsageSummary]:
        """Get usage data for the current billing period (current month).

        Args:
            month: Optional datetime to specify which month. Defaults to current month.
            app: Optional app name to filter results.

        Returns:
            List of AppUsageSummary objects, one per app with their usage records.
        """
        # Calculate current month date range
        now = month or datetime.now()

        # Start of month
        start_date = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        # End date: most recent date we can query (cannot be today)
        end_date = datetime.now() - timedelta(days=1)

        # If we're looking at a past month, use the last day of that month
        if month and month.month != datetime.now().month:
            # Get last day of the specified month
            if month.month == 12:
                next_month = month.replace(year=month.year + 1, month=1)
            else:
                next_month = month.replace(month=month.month + 1)
            end_date = next_month - timedelta(days=1)

        # Make sure we don't exceed the 31-day API limit
        date_diff = (end_date - start_date).days
        if date_diff > 31:
            # If the range is too long, start from 31 days before end_date
            start_date = end_date - timedelta(days=30)

        # Use the general get_usage_for_period method
        return await self.get_usage_for_period(
            start_date=start_date,
            end_date=end_date,
            app=app,
        )
