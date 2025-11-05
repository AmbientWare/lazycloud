from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical

from lazycloud_cli.config import config
from lazycloud_cli.ui.textual.components import Container

from .deployment_breakdown import DeploymentBreakdownSection
from .deployments_list import DeploymentsListView
from .overview import UsageOverviewSection


class UsageMainContainer(Container):
    """Main container for usage dashboard"""

    def __init__(self):
        super().__init__(id="usage-main-container")
        workspace_name = config.active_workspace_name or config.active_workspace_id[:8]
        self.border_title = f"Workspace: {workspace_name}"
        self.border_subtitle = "1: Deployments • r: Refresh • q: Quit"
        self._overview: UsageOverviewSection | None = None
        self._deployments_list: DeploymentsListView | None = None
        self._breakdown: DeploymentBreakdownSection | None = None
        self._workspace_name = workspace_name

    def compose(self) -> ComposeResult:
        """Compose the container contents"""
        # Top section: overview
        with Vertical(id="usage-top-section"):
            self._overview = UsageOverviewSection()
            yield self._overview

        # Bottom section: deployments list (left) | breakdown (right)
        with Horizontal(id="usage-horizontal-split"):
            self._deployments_list = DeploymentsListView()
            yield self._deployments_list

            # Right side: deployment breakdown
            with Vertical(id="usage-right-panel"):
                self._breakdown = DeploymentBreakdownSection()
                yield self._breakdown

    def on_mount(self) -> None:
        """Watch for deployment selection changes and usage data updates"""
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

    def _on_usage_data_loaded(self, usage_data) -> None:
        """Handle usage data loaded - share with breakdown component and update title"""
        if self._breakdown:
            self._breakdown.usage_data = usage_data

        # Update main container title with date range
        if usage_data and usage_data.period:
            period_start = usage_data.period.start.strftime("%b %d")
            period_end = usage_data.period.end.strftime("%b %d, %Y")
            self.border_title = (
                f"Workspace: {self._workspace_name} • {period_start} - {period_end}"
            )
        else:
            self.border_title = f"Workspace: {self._workspace_name}"

    def _on_deployment_selected(self, deployment_id: str | None) -> None:
        """Handle deployment selection change"""
        if self._overview:
            self._overview.selected_deployment_id = deployment_id
        if self._breakdown:
            self._breakdown.selected_deployment_id = deployment_id
