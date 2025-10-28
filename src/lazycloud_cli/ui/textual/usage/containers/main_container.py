from datetime import datetime

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive

from lazycloud_cli.config import config
from lazycloud_cli.ui.textual.components import Container
from shared.responses.usage import WorkspaceUsageResponse

from .deployments_list import DeploymentsListView
from .overview import UsageOverviewSection
from .service_breakdown import UsageBreakdownTable
from .sparkline import UsageTrendSparkline
from .volume_usage import VolumeUsageTable


class UsageMainContainer(Container):
    """Main container for usage dashboard"""

    # Shared billing period for all components
    period_start: reactive[datetime | None] = reactive(None)
    period_end: reactive[datetime | None] = reactive(None)

    def __init__(self):
        super().__init__(id="usage-main-container")
        workspace_name = config.active_workspace_name or config.active_workspace_id[:8]
        self.border_title = f"Workspace: {workspace_name}"
        self.border_subtitle = "1: Deployments • 2: Services • r: Refresh • q: Quit"
        self._overview: UsageOverviewSection | None = None
        self._sparkline: UsageTrendSparkline | None = None
        self._deployments_list: DeploymentsListView | None = None
        self._breakdown_table: UsageBreakdownTable | None = None
        self._volume_table: VolumeUsageTable | None = None

    def compose(self) -> ComposeResult:
        """Compose the container contents"""
        # Top section: billing period stacked above sparkline (both full width)
        with Vertical(id="usage-top-section"):
            self._overview = UsageOverviewSection()
            self._sparkline = UsageTrendSparkline()
            yield self._overview
            yield self._sparkline

        # Bottom section: deployments list (25%) | right panel (75%)
        with Horizontal(id="usage-horizontal-split"):
            self._deployments_list = DeploymentsListView()
            yield self._deployments_list

            # Right side container for stacked components
            with Vertical(id="usage-right-panel"):
                self._breakdown_table = UsageBreakdownTable()
                self._volume_table = VolumeUsageTable()
                yield self._breakdown_table
                yield self._volume_table

    def on_mount(self) -> None:
        """Watch for deployment selection changes and period updates"""
        if self._deployments_list:
            self.watch(
                self._deployments_list,
                "selected_deployment_id",
                self._on_deployment_selected,
            )
        if self._overview:
            self.watch(
                self._overview,
                "usage_data",
                self._on_usage_data_loaded,
            )

    def _on_usage_data_loaded(self, usage_data: WorkspaceUsageResponse | None) -> None:
        """Handle usage data loaded - extract and share billing period"""
        if usage_data and usage_data.period:
            self.period_start = usage_data.period.start
            self.period_end = usage_data.period.end

            # Update sparkline with period dates
            if self._sparkline:
                self._sparkline.period_start = self.period_start
                self._sparkline.period_end = self.period_end

    def _on_deployment_selected(self, deployment_id: str | None) -> None:
        """Handle deployment selection change"""
        if deployment_id:
            # Update service breakdown with period dates
            if self._breakdown_table:
                self.run_worker(
                    self._breakdown_table.fetch_service_usage(
                        deployment_id, self.period_start, self.period_end
                    ),
                    exclusive=True,
                )
            # Update volume usage with period dates
            if self._volume_table:
                self._volume_table.deployment_id = deployment_id
                self._volume_table.period_start = self.period_start
                self._volume_table.period_end = self.period_end

