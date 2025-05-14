import httpx
import json
from datetime import datetime, timedelta
import asyncio

from src.config import app_config
from src.database import db
from src.services.fly import FlyAppManager


class UsageManager:
    """Class to collect and process Fly.io metrics."""

    def __init__(self):
        """Initialize the collector with organization slug."""
        self.client: httpx.AsyncClient | None = httpx.AsyncClient()
        self.base_url: str = (
            f"https://api.fly.io/prometheus/{app_config.FLY_ORG_NAME}/api/v1/query_range"
        )
        self.fly_manager = FlyAppManager()

    def _get_today_time_range(self):
        """Get the time range for today up to current time."""
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        current_time = datetime.now()

        return today, current_time

    async def _fetch_instance_metrics(
        self,
        start_time: datetime,
        end_time: datetime,
        usage_uuid: str,
    ):
        """Fetch both instance and data out metrics."""
        if not self.client:
            raise ValueError("Client not initialized")

        headers = {"Authorization": f"{app_config.FLY_API_TOKEN}"}

        # Fetch instance metrics with filter for apps containing the usage_uuid
        instance_params = {
            "query": f'sum by (memory_mb, cpu_count, cpu_kind, app, region) (count by (memory_mb, cpu_count, cpu_kind, app, region, instance_id) (fly_instance_info{{app=~"lc-{usage_uuid}-.*"}}))',
            "start": int(start_time.timestamp()),
            "end": int(end_time.timestamp()),
            "step": "1m",
        }

        data_out_params = {
            "query": f'sum by (app) (fly_edge_data_out{{app=~"lc-{usage_uuid}-.*"}})',
            "start": int(start_time.timestamp()),
            "end": int(end_time.timestamp()),
            "step": "1m",
        }

        # Make both requests concurrently
        instance_response, data_out_response = await asyncio.gather(
            self.client.get(self.base_url, params=instance_params, headers=headers),
            self.client.get(self.base_url, params=data_out_params, headers=headers),
        )

        return instance_response.json(), data_out_response.json()

    async def _combine_metrics(self, instance_data: dict, data_out_data: dict):
        """Combine instance and data out metrics into a dictionary with app names as keys."""
        combined = {}

        # Create a map of app to data out metrics
        data_out_map = {}
        for result in data_out_data.get("data", {}).get("result", []):
            app = result["metric"]["app"]
            total_bytes = sum(float(value[1]) for value in result["values"])
            data_out_map[app] = total_bytes

        # Combine metrics
        for result in instance_data.get("data", {}).get("result", []):
            app = result["metric"]["app"]
            total_minutes = sum(float(value[1]) for value in result["values"])

            # Initialize app entry if it doesn't exist
            if app not in combined:
                # fetch the volume info for the app
                combined[app] = {
                    "total_bytes_out": data_out_map.get(app, 0),
                    "volume_size": None,  # just initialize to None for now
                    "volume_region": None,  # just initialize to None for now
                    "configurations": [],
                }

            # Add configuration to app's configurations list
            combined[app]["configurations"].append(
                {
                    "memory_mb": int(result["metric"]["memory_mb"]),
                    "cpu_count": int(result["metric"]["cpu_count"]),
                    "cpu_kind": result["metric"]["cpu_kind"],
                    "uptime": total_minutes,
                    "region": result["metric"]["region"],
                }
            )

        # get the volume size and region for all the apps
        # do this asynchronously with asyncio.gather since its an api call
        promises = [
            self.fly_manager.get_volume_info(app, volume_id)
            for app in combined.keys()
            for volume_id in combined[app]["configurations"]
        ]
        volume_results = await asyncio.gather(*promises)

        for app, (volume_size, volume_region) in zip(combined.keys(), volume_results):
            combined[app]["volume_size"] = volume_size
            combined[app]["volume_region"] = volume_region

        # Sort configurations by memory_mb, cpu_count, and cpu_kind
        for app_data in combined.values():
            app_data["configurations"].sort(
                key=lambda x: (x["memory_mb"], x["cpu_count"], x["cpu_kind"])
            )

        return combined

    def get_time_range(self, start_time: datetime, end_time: datetime):
        """Get the time range for two days ago."""
        return start_time, end_time

    async def collect_metrics(self, start_time: datetime, end_time: datetime):
        """Main method to collect and process all metrics."""
        try:
            # Get organization and token
            print(f"Using organization: {app_config.FLY_ORG_NAME}")

            # Get time range
            start_time, end_time = self.get_time_range(start_time, end_time)
            print(
                f"Fetching combined metrics from {start_time.isoformat()} to {end_time.isoformat()}:"
            )

            # get all the usage db entries
            usage_entries = await db.usage.afind(filters={})

            all_combined_metrics = {}

            for usage_entry in usage_entries:
                print(f"Collecting metrics for usage entry: {usage_entry}")
                usage_uuid = usage_entry.uuid

                if not usage_uuid:
                    print(f"Skipping usage entry with no UUID: {usage_entry}")
                    continue

                # Fetch and combine metrics
                instance_data, data_out_data = await self._fetch_instance_metrics(
                    start_time, end_time, usage_uuid
                )

                combined_metrics = await self._combine_metrics(
                    instance_data, data_out_data
                )
                all_combined_metrics.update(combined_metrics)

            return all_combined_metrics
        finally:
            if self.client:
                await self.client.aclose()


async def main():
    usage_manager = UsageManager()
    # Get today's metrics
    print("\n=== Today's Metrics ===")
    start_time = datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - timedelta(days=5)
    end_time = datetime.now()
    today_metrics = await usage_manager.collect_metrics(start_time, end_time)
    print(json.dumps(today_metrics, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
