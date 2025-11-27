import asyncio
from datetime import datetime, timezone

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widgets import DataTable

from lazycloud_cli.api.usage import UsageAPI
from lazycloud_cli.config import config
from lazycloud_cli.ui.colors import Colors
from lazycloud_cli.ui.textual.components import Container
from lazycloud_cli.ui.textual.components.section import SectionContainer
from lazycloud_cli.ui.textual.theme import Icons
from shared.responses.usage import (
    UsagePeriodInfo,
    WorkspaceUsageSummary,
)


class UsageOverviewSection(Container):
    """Section showing workspace usage summary"""

    usage_data: reactive[WorkspaceUsageSummary | None] = reactive(None)
    usage_period: reactive[UsagePeriodInfo | None] = reactive(None)

    def __init__(self):
        super().__init__(id="usage-overview-section")
        self._usage_api = UsageAPI()
        self._section_container: SectionContainer | None = None
        self._metrics_table: DataTable | None = None
        self._update_retry_count = 0

    def compose(self) -> ComposeResult:
        """Compose the usage overview"""
        self._section_container = SectionContainer(f"{Icons.COMPUTER} Usage Summary")
        with self._section_container:
            self._metrics_table = DataTable(
                show_header=True,
                id="workspace-totals-table",
                show_cursor=False,
                zebra_stripes=True,
            )
            self._metrics_table.can_focus = False
            self._metrics_table.add_columns("Metric", "Usage (unit-h)", "Cost ($)")
            # Show loading state
            self._metrics_table.add_row(
                "Loading...",
                "",
                "",
                key="loading",
            )
            yield self._metrics_table

    def on_mount(self) -> None:
        """Fetch usage data when mounted"""
        now_local = datetime.now()
        start_local = now_local.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        start_date = start_local.astimezone(timezone.utc).replace(microsecond=0)
        end_date = datetime.now(timezone.utc).replace(microsecond=0)

        self.run_worker(self._fetch_usage_async(start_date, end_date), exclusive=True)

        # Ensure display updates after widget is fully mounted
        # This handles cases where data arrives before mount completes
        self.call_after_refresh(self._try_update_display)

    async def _fetch_usage_async(
        self,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> None:
        """Fetch aggregated usage and filter for active workspace"""
        try:
            aggregated = await asyncio.to_thread(
                self._usage_api.get_aggregated_usage,
                start_date=start_date,
                end_date=end_date,
            )
            self.usage_period = aggregated.period

            # Find workspace summary for active workspace
            workspace_summary = None
            for ws in aggregated.workspaces:
                if ws.workspace_id == config.active_workspace_id:
                    workspace_summary = ws
                    break

            if not workspace_summary:
                raise Exception(
                    f"Workspace {config.active_workspace_id} not found in usage data"
                )

            self.usage_data = workspace_summary
        except Exception as e:
            self.log.error(f"Failed to fetch usage data: {e}", exc_info=True)
            if self._metrics_table:
                self._metrics_table.clear()
                self._metrics_table.add_row(
                    "Error",
                    str(e),
                    "-",
                    key="error",
                )

    def watch_usage_data(self, usage: WorkspaceUsageSummary | None) -> None:
        """Update display when usage data changes"""
        if usage:
            # Always schedule update - call_after_refresh will handle timing
            self.call_after_refresh(self._try_update_display)

    def _try_update_display(self) -> None:
        """Try to update display - will update if table is ready and data is available"""
        # Wait for table to be ready
        if not self._metrics_table or not self._metrics_table.is_mounted:
            # Retry after a short delay if table isn't ready yet (max 10 retries = 1 second)
            if self.usage_data and self._update_retry_count < 10:
                self._update_retry_count += 1
                self.set_timer(0.1, self._try_update_display)
            return

        # Reset retry count on success
        self._update_retry_count = 0

        if not self.usage_data:
            return

        self._update_display()

    def _update_display(self) -> None:
        """Update the display to always show workspace totals"""

        usage = self.usage_data

        # Always show workspace totals
        metrics = usage.usage
        costs = metrics.costs
        if self._section_container:
            self._section_container.border_title = f"{Icons.COMPUTER} Workspace Totals"

        # Clear and update the table
        self._metrics_table.clear()

        # CPU row
        cpu_cost_str = f"{costs.cpu_cost:.2f}" if costs else "-"
        self._metrics_table.add_row(
            "CPU (core)",
            f"{metrics.cpu_core_hours:.2f}",
            cpu_cost_str,
            key="cpu",
        )

        # Memory row
        memory_cost_str = f"{costs.memory_cost:.2f}" if costs else "-"
        self._metrics_table.add_row(
            "Memory (GB)",
            f"{metrics.memory_gb_hours:.2f}",
            memory_cost_str,
            key="memory",
        )

        # Storage row (combined EBS + EFS)
        storage_hours = metrics.standard_gb_hours + metrics.shared_gb_hours
        storage_cost = (costs.standard_cost + costs.shared_cost) if costs else None
        storage_cost_str = f"{storage_cost:.2f}" if storage_cost is not None else "-"
        self._metrics_table.add_row(
            "Storage (GB)",
            f"{storage_hours:.2f}",
            storage_cost_str,
            key="storage",
        )

        # Build Minutes row
        build_cost_str = f"{costs.build_cost:.2f}" if costs else "-"
        self._metrics_table.add_row(
            "Build Minutes",
            f"{metrics.build_minutes:.2f}",
            build_cost_str,
            key="build",
        )

        # Endpoints row
        endpoint_cost_str = f"{costs.endpoint_cost:.2f}" if costs else "-"
        self._metrics_table.add_row(
            "Endpoints (hours)",
            f"{metrics.public_endpoint_hours:.2f}",
            endpoint_cost_str,
            key="endpoints",
        )

        # Total row
        if costs:
            success_color = Colors.Hex.success
            total_cost_str = f"{costs.total_cost:.2f}"
            self._metrics_table.add_row(
                f"[bold {success_color}]Total[/bold {success_color}]",
                "",
                f"[bold {success_color}]{total_cost_str}[/bold {success_color}]",
                key="total",
            )

    def refresh_usage(
        self, start_date: datetime | None = None, end_date: datetime | None = None
    ) -> None:
        """Manually refresh usage data"""
        self.run_worker(self._fetch_usage_async(start_date, end_date), exclusive=True)
