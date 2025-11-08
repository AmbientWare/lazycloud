from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical

from lazycloud_cli.config import config
from lazycloud_cli.ui.textual.components import Container
from lazycloud_cli.ui.textual.components.listview import ListItem
from shared.responses.usage import UsagePeriodInfo, WorkspaceUsageSummary

from .breakdown import DeploymentBreakdownSection
from .deployments_list import ActiveDeploymentsContainer, InactiveDeploymentsContainer
from .overview import UsageOverviewSection


class UsageMainContainer(Container):
    """Main container for usage dashboard"""

    BINDINGS = [
        ("1", "focus_active", "Focus Active"),
        ("2", "focus_inactive", "Focus Inactive"),
        ("r", "refresh", "Refresh"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self):
        super().__init__(id="usage-main-container")
        workspace_name = config.active_workspace_name or config.active_workspace_id[:8]
        self.border_title = f"Workspace: {workspace_name}"
        self.border_subtitle = "1: Active • 2: Inactive • r: Refresh • q: Quit"
        self._overview: UsageOverviewSection | None = None
        self._active_deployments: ActiveDeploymentsContainer | None = None
        self._inactive_deployments: InactiveDeploymentsContainer | None = None
        self._breakdown: DeploymentBreakdownSection | None = None
        self._workspace_name = workspace_name

    def compose(self) -> ComposeResult:
        """Compose the container contents"""
        # Top section: overview
        with Vertical(id="usage-top-section"):
            self._overview = UsageOverviewSection()
            yield self._overview

        # Bottom section: deployments lists (left) | breakdown (right)
        with Horizontal(id="usage-horizontal-split"):
            # Left side: two stacked deployment containers
            with Vertical(id="usage-left-panel"):
                self._active_deployments = ActiveDeploymentsContainer()
                yield self._active_deployments
                self._inactive_deployments = InactiveDeploymentsContainer()
                yield self._inactive_deployments

            # Right side: deployment breakdown
            with Vertical(id="usage-right-panel"):
                self._breakdown = DeploymentBreakdownSection()
                yield self._breakdown

    def on_mount(self) -> None:
        """Watch for deployment selection changes and usage data updates"""
        if self._active_deployments:
            self.watch(
                self._active_deployments,
                "selected_deployment_id",
                self._on_deployment_selected,
            )
        if self._inactive_deployments:
            self.watch(
                self._inactive_deployments,
                "selected_deployment_id",
                self._on_deployment_selected,
            )
        if self._overview:
            self.watch(
                self._overview,
                "usage_data",
                self._on_usage_data_loaded,
            )
            self.watch(
                self._overview,
                "usage_period",
                self._on_period_changed,
            )

    def _on_usage_data_loaded(self, usage_data: WorkspaceUsageSummary | None) -> None:
        """Handle usage data loaded - share with breakdown component and deployments lists"""
        if self._breakdown:
            self._breakdown.usage_data = usage_data
            if self._overview:
                self._breakdown.usage_period = self._overview.usage_period
        if self._active_deployments:
            self._active_deployments.usage_data = usage_data
        if self._inactive_deployments:
            self._inactive_deployments.usage_data = usage_data

        # Auto-select first deployment (prefer active, fallback to inactive)
        if usage_data and usage_data.deployments:
            active_deployments = [
                d for d in usage_data.deployments if d.status == "Active"
            ]
            inactive_deployments = [
                d for d in usage_data.deployments if d.status == "Inactive"
            ]

            # Prefer active deployments
            if active_deployments and self._active_deployments:
                if not self._active_deployments.selected_deployment_id:
                    first_deployment = active_deployments[0]
                    deployment_id = first_deployment.deployment_id
                    self._active_deployments.selected_deployment_id = deployment_id
                    # Also set breakdown selection to ensure it updates
                    if self._breakdown:
                        self._breakdown.selected_deployment_id = deployment_id
                    # Focus active container
                    self.call_after_refresh(
                        lambda: self.app.set_focus(self._active_deployments)
                    )
            elif inactive_deployments and self._inactive_deployments:
                if not self._inactive_deployments.selected_deployment_id:
                    first_deployment = inactive_deployments[0]
                    deployment_id = first_deployment.deployment_id
                    self._inactive_deployments.selected_deployment_id = deployment_id
                    # Also set breakdown selection to ensure it updates
                    if self._breakdown:
                        self._breakdown.selected_deployment_id = deployment_id
                    # Focus inactive container
                    self.call_after_refresh(
                        lambda: self.app.set_focus(self._inactive_deployments)
                    )

    def _on_period_changed(self, period: UsagePeriodInfo | None) -> None:
        """Update main container title with date range and share period with breakdown"""
        if period:
            period_start = period.start.strftime("%b %d")
            period_end = period.end.strftime("%b %d, %Y")
            self.border_title = (
                f"Workspace: {self._workspace_name} • {period_start} - {period_end}"
            )
        else:
            self.border_title = f"Workspace: {self._workspace_name}"

        if self._breakdown:
            self._breakdown.usage_period = period

    def _on_deployment_selected(self, deployment_id: str | None) -> None:
        """Handle deployment selection change"""
        if self._breakdown:
            self._breakdown.selected_deployment_id = deployment_id

    def action_focus_active(self) -> None:
        """Focus the active deployments container"""
        if self._active_deployments:
            self.app.set_focus(self._active_deployments)
            # Use call_after_refresh to ensure selection happens after focus is set
            self.call_after_refresh(self._update_active_selection)

    def action_focus_inactive(self) -> None:
        """Focus the inactive deployments container"""
        if self._inactive_deployments:
            self.app.set_focus(self._inactive_deployments)
            # Use call_after_refresh to ensure selection happens after focus is set
            self.call_after_refresh(self._update_inactive_selection)

    def _update_active_selection(self) -> None:
        """Update selection for active deployments after focus"""
        if not self._active_deployments or not self._active_deployments._list_view:
            return

        deployment_id = None
        # Get currently highlighted item
        if self._active_deployments._list_view.index is not None:
            idx = self._active_deployments._list_view.index
            if idx < len(self._active_deployments._list_view.children):
                item = self._active_deployments._list_view.children[idx]
                if isinstance(item, ListItem) and item.item_data:
                    deployment_id = item.item_data.id
                    self._active_deployments.selected_deployment_id = deployment_id
        # If no highlighted item, select first item
        if (
            deployment_id is None
            and len(self._active_deployments._list_view.children) > 0
        ):
            first_item = self._active_deployments._list_view.children[0]
            if isinstance(first_item, ListItem) and first_item.item_data:
                deployment_id = first_item.item_data.id
                self._active_deployments.selected_deployment_id = deployment_id
                self._active_deployments._list_view.index = 0
        # Directly update breakdown to ensure it refreshes even if reactive property doesn't fire
        if deployment_id and self._breakdown:
            self._breakdown.selected_deployment_id = deployment_id

    def _update_inactive_selection(self) -> None:
        """Update selection for inactive deployments after focus"""
        if not self._inactive_deployments or not self._inactive_deployments._list_view:
            return

        deployment_id = None
        # Get currently highlighted item
        if self._inactive_deployments._list_view.index is not None:
            idx = self._inactive_deployments._list_view.index
            if idx < len(self._inactive_deployments._list_view.children):
                item = self._inactive_deployments._list_view.children[idx]
                if isinstance(item, ListItem) and item.item_data:
                    deployment_id = item.item_data.id
                    self._inactive_deployments.selected_deployment_id = deployment_id
        # If no highlighted item, select first item
        if (
            deployment_id is None
            and len(self._inactive_deployments._list_view.children) > 0
        ):
            first_item = self._inactive_deployments._list_view.children[0]
            if isinstance(first_item, ListItem) and first_item.item_data:
                deployment_id = first_item.item_data.id
                self._inactive_deployments.selected_deployment_id = deployment_id
                self._inactive_deployments._list_view.index = 0
        # Directly update breakdown to ensure it refreshes even if reactive property doesn't fire
        if deployment_id and self._breakdown:
            self._breakdown.selected_deployment_id = deployment_id

    def action_refresh(self) -> None:
        """Refresh usage data"""
        if self._overview:
            self._overview.refresh_usage()

    def action_quit(self) -> None:
        """Quit the usage dashboard"""
        self.app.exit()
