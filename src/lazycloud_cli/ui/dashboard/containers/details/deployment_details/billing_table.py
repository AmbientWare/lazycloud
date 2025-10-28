import asyncio

from rich.text import Text
from textual.reactive import reactive
from textual.widgets import DataTable

from lazycloud_cli.api.usage import UsageAPI
from lazycloud_cli.config import config
from shared.responses.usage import CurrentUsageResponse


class BillingTable(DataTable):
    """Widget to display current usage and billing information"""

    current_usage: reactive[CurrentUsageResponse | None] = reactive(None)
    error_message: reactive[str | None] = reactive(None)

    def __init__(self, **kwargs):
        super().__init__(show_cursor=False, zebra_stripes=True, **kwargs)
        self._usage_api = UsageAPI()
        self.add_columns("Metric", "Current Usage", "Unit")

    def on_mount(self) -> None:
        """Start fetching usage data when mounted"""
        self._fetch_usage_data()

    def watch_current_usage(self, new_value: CurrentUsageResponse | None) -> None:
        """React to usage data changes"""
        if new_value:
            self._update_table(new_value)

    def watch_error_message(self, new_value: str | None) -> None:
        """React to error message changes"""
        if new_value:
            self._show_error(new_value)

    def _fetch_usage_data(self) -> None:
        """Fetch current usage data from the API"""
        if not config.active_workspace_id:
            self.error_message = "No active workspace"
            return

        self.run_worker(self._get_usage_async(), exclusive=True)

    async def _get_usage_async(self) -> None:
        """Async worker to fetch usage data"""
        try:
            # Run the sync API call in a thread to avoid blocking
            usage = await asyncio.to_thread(
                self._usage_api.get_current_usage, config.active_workspace_id
            )
            self.current_usage = usage
        except Exception as e:
            self.error_message = f"Failed to fetch usage data: {str(e)}"

    def _update_table(self, usage: CurrentUsageResponse) -> None:
        """Update the table with usage data"""
        self.clear()

        # Format timestamp
        timestamp_str = usage.timestamp.strftime("%Y-%m-%d %H:%M:%S")

        # Add rows with current usage
        rows = [
            ("💻 CPU Cores", f"{usage.current_usage.cpu_cores:.3f}", "cores"),
            ("🧠 Memory", f"{usage.current_usage.memory_gb:.2f}", "GB"),
            ("💾 Storage", f"{usage.current_usage.storage_gb:.2f}", "GB"),
            ("🕒 Last Updated", timestamp_str, ""),
        ]

        for metric, value, unit in rows:
            self.add_row(
                Text(metric, style="bold cyan"),
                Text(value, style="bold green"),
                Text(unit, style="dim"),
            )

    def _show_error(self, error: str) -> None:
        """Show error message in the table"""
        self.clear()
        self.add_row(
            Text("Error", style="bold red"),
            Text(error, style="red"),
            Text("", style="dim"),
        )

    def refresh_data(self) -> None:
        """Manually refresh usage data"""
        self._fetch_usage_data()
