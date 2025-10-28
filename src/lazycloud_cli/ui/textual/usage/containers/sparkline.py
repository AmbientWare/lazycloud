import asyncio
from datetime import datetime

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widgets import Sparkline

from lazycloud_cli.api.usage import UsageAPI
from lazycloud_cli.config import config
from lazycloud_cli.ui.textual.components import Container
from lazycloud_cli.ui.textual.components.section import SectionContainer
from lazycloud_cli.ui.textual.theme import Icons
from shared.responses.usage import DailyUsageResponse


class UsageTrendSparkline(Container):
    """Sparkline showing daily usage trends"""

    daily_data: reactive[DailyUsageResponse | None] = reactive(None)
    period_start: reactive[datetime | None] = reactive(None)
    period_end: reactive[datetime | None] = reactive(None)

    def __init__(self):
        super().__init__(id="usage-trend-section")
        self._usage_api = UsageAPI()
        self._sparkline: Sparkline | None = None

    def compose(self) -> ComposeResult:
        """Compose the sparkline section"""
        with SectionContainer(f"{Icons.TREND} Daily Usage Trend"):
            self._sparkline = Sparkline(
                [],
                summary_function=max,
                id="usage-sparkline",
            )
            yield self._sparkline

    def on_mount(self) -> None:
        """Fetch daily usage when mounted"""
        self.run_worker(self._fetch_daily_usage_async(), exclusive=True)

    def watch_period_start(self, period_start: datetime | None) -> None:
        """Refetch when period changes"""
        if period_start and self.period_end:
            self.run_worker(self._fetch_daily_usage_async(), exclusive=True)

    async def _fetch_daily_usage_async(self) -> None:
        """Fetch daily usage from the API using the shared billing period"""
        try:
            daily_usage = await asyncio.to_thread(
                self._usage_api.get_daily_usage,
                config.active_workspace_id,
                start_date=self.period_start,
                end_date=self.period_end,
            )
            self.daily_data = daily_usage
        except Exception:
            self.log.error("Failed to fetch daily usage")
            pass

    def watch_daily_data(self, daily: DailyUsageResponse | None) -> None:
        """Update sparkline when data changes"""
        if not daily or not self._sparkline:
            return

        # Sum total usage for each day (CPU + Memory + Storage)
        daily_usage = []
        # TODO: we should swith this to cost based when we have that info.
        for day in daily.daily_usage:
            total = (
                day.cpu_core_hours
                + day.memory_gb_hours
                + day.s3_gb_hours
                + day.efs_gb_hours
            )
            daily_usage.append(max(total, 0.0))  # Ensure non-negative

        # Normalize data to 0-100 scale for better visualization
        if daily_usage and max(daily_usage) > 0:
            min_usage = min(daily_usage)
            max_usage = max(daily_usage)
            usage_range = max_usage - min_usage

            if usage_range > 0:
                # Normalize to 0-100 scale based on range
                normalized = [(u - min_usage) / usage_range * 100 for u in daily_usage]
            else:
                # All values are the same, use a constant value
                normalized = [0.0] * len(daily_usage)

            self._sparkline.data = normalized
        else:
            # No data or all zeros
            self._sparkline.data = [0.0]

    def refresh_trend(self) -> None:
        """Manually refresh trend data"""
        self.run_worker(self._fetch_daily_usage_async(), exclusive=True)

