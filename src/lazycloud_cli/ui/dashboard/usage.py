"""Usage and billing dashboard for LazyCloud CLI"""

import asyncio
from textwrap import dedent

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import DataTable, Sparkline, Static

from lazycloud_cli.api import api
from lazycloud_cli.api.usage import UsageAPI
from lazycloud_cli.config import config
from lazycloud_cli.ui.colors import Colors
from lazycloud_cli.ui.dashboard.components import Container
from lazycloud_cli.ui.dashboard.components.listview import ListItemData, ListView
from lazycloud_cli.ui.dashboard.components.section import SectionContainer
from lazycloud_cli.ui.dashboard.theme import Icons, lazycloud_theme
from shared.models.billing import STORAGE_CLASS_TO_TYPE
from shared.responses.usage import DailyUsageResponse, WorkspaceUsageResponse


class UsageOverviewSection(Container):
    """Section showing current billing period usage"""

    usage_data: reactive[WorkspaceUsageResponse | None] = reactive(None)

    def __init__(self):
        super().__init__(id="usage-overview-section")
        self._usage_api = UsageAPI()
        self._content_widget: Static | None = None

    def compose(self) -> ComposeResult:
        """Compose the usage overview"""
        with SectionContainer(
            f"{Icons.COMPUTER}  Current Billing Period"
        ):  # extra space after icon for readability
            self._content_widget = Static("Loading usage data...", id="usage-content")
            yield self._content_widget

    def on_mount(self) -> None:
        """Fetch usage data when mounted"""
        self.run_worker(self._fetch_usage_async(), exclusive=True)

    async def _fetch_usage_async(self) -> None:
        """Fetch current period usage from the API"""
        try:
            usage = await asyncio.to_thread(
                self._usage_api.get_usage, config.active_workspace_id
            )
            self.usage_data = usage
        except Exception as e:
            if self._content_widget:
                self._content_widget.update(
                    f"[{Colors.Hex.error}]{str(e)}[/{Colors.Hex.error}]"
                )

    def watch_usage_data(self, usage: WorkspaceUsageResponse | None) -> None:
        """Update display when usage data changes"""
        if not usage or not self._content_widget:
            return

        period_start = usage.period.start.strftime("%b %d")
        period_end = usage.period.end.strftime("%b %d, %Y")
        accent = Colors.Hex.accent

        # Display metrics on two rows
        text = (
            f"[bold {accent}]CPU:[/bold {accent}] {usage.usage.cpu_core_hours:.2f} core-hrs  "
            f"[bold {accent}]Memory:[/bold {accent}] {usage.usage.memory_gb_hours:.2f} GB-hrs\n"
            f"[bold {accent}]Standard Storage:[/bold {accent}] {usage.usage.s3_gb_hours:.2f} GB-hrs  "
            f"[bold {accent}]High Performance Storage:[/bold {accent}] {usage.usage.efs_gb_hours:.2f} GB-hrs\n"
            f"[dim]Period: {period_start} - {period_end}[/dim]"
        )

        self._content_widget.update(text)

    def refresh_usage(self) -> None:
        """Manually refresh usage data"""
        self.run_worker(self._fetch_usage_async(), exclusive=True)


class UsageTrendSparkline(Container):
    """Sparkline showing daily usage trends"""

    daily_data: reactive[DailyUsageResponse | None] = reactive(None)

    def __init__(self):
        super().__init__(id="usage-trend-section")
        self._usage_api = UsageAPI()
        self._sparkline: Sparkline | None = None

    def compose(self) -> ComposeResult:
        """Compose the sparkline section"""
        with SectionContainer("📈 Usage Trend (Daily Total)"):
            self._sparkline = Sparkline(
                [],
                summary_function=max,
                id="usage-sparkline",
            )
            yield self._sparkline

    def on_mount(self) -> None:
        """Fetch daily usage when mounted"""
        self.run_worker(self._fetch_daily_usage_async(), exclusive=True)

    async def _fetch_daily_usage_async(self) -> None:
        """Fetch daily usage from the API"""
        try:
            daily_usage = await asyncio.to_thread(
                self._usage_api.get_daily_usage, config.active_workspace_id
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


class VolumeUsageTable(DataTable):
    """Table showing volume usage breakdown"""

    deployment_id: reactive[str | None] = reactive(None)

    BINDINGS = [
        ("up,k", "cursor_up", "Move up"),
        ("down,j", "cursor_down", "Move down"),
    ]

    def __init__(self):
        super().__init__(
            zebra_stripes=True,
            id="volume-usage-table",
        )
        self.border_title = "💾 Storage Usage"
        self.cursor_type = "row"
        self.show_cursor = True
        self.can_focus = True
        self.add_columns(
            "Name",
            "Type",
            "Usage (GB-hrs)",
        )
        self._usage_api = UsageAPI()

    def on_mount(self) -> None:
        """Show initial state"""
        self.show_row_labels = False
        self._show_message("Select a deployment to view volumes")

    def on_focus(self) -> None:
        """Handle focus event"""
        self.border_subtitle = "↑↓/jk Navigate"

    def on_blur(self) -> None:
        """Handle blur event"""
        self.border_subtitle = ""

    def watch_deployment_id(self, deployment_id: str | None) -> None:
        """Update when deployment changes"""
        if deployment_id:
            self.run_worker(self._fetch_volume_usage(deployment_id), exclusive=True)
        else:
            self._show_message("Select a deployment to view volumes")

    def _show_message(self, message: str) -> None:
        """Show a message in the table"""
        self.clear()
        self.add_row(
            f"[dim]{message}[/dim]",
            "",
            "",
        )

    async def _fetch_volume_usage(self, deployment_id: str) -> None:
        """Fetch volume usage for a deployment"""
        try:
            # Get usage data with deployment_id for detailed breakdown
            usage = await asyncio.to_thread(
                self._usage_api.get_usage,
                config.active_workspace_id,
                deployment_id=deployment_id,
            )

            # Clear and populate table
            self.clear()

            if usage.volumes and len(usage.volumes) > 0:
                for volume in usage.volumes:
                    storage_type = STORAGE_CLASS_TO_TYPE.get(
                        volume.storage_class, volume.storage_class
                    )
                    self.add_row(
                        volume.volume_name,
                        storage_type,
                        f"{volume.gb_hours:.2f}",
                    )
            else:
                self._show_message("No storage volumes found")

            # Position cursor on first row
            if self.row_count > 0:
                self.move_cursor(row=0)

        except Exception as e:
            self._show_message(str(e))


class DeploymentsListView(Container):
    """List view showing workspace deployments"""

    selected_deployment_id: reactive[str | None] = reactive(None)

    BINDINGS = [
        ("up,k", "cursor_up", "Move up"),
        ("down,j", "cursor_down", "Move down"),
    ]

    def __init__(self):
        super().__init__(id="deployments-list-container")
        self._list_view: ListView | None = None
        self.border_title = f"{Icons.ROCKET} [1] Deployments"

    def compose(self) -> ComposeResult:
        """Compose the list view"""
        self._list_view = ListView(id="deployments-list")
        yield self._list_view

    def on_mount(self) -> None:
        """Fetch deployments when mounted"""
        self.can_focus = True
        self.run_worker(self._fetch_deployments_async(), exclusive=True)

    def on_focus(self) -> None:
        """Handle focus event"""
        self.border_subtitle = "↑↓/jk Navigate"
        if self._list_view:
            self._list_view.ensure_highlighted()

    def on_blur(self) -> None:
        """Handle blur event"""
        self.border_subtitle = ""

    async def _fetch_deployments_async(self) -> None:
        """Fetch deployments from API"""
        try:
            if not self._list_view:
                return

            self._list_view.show_loading("Loading deployments...")

            # Fetch deployments using existing API
            response = await asyncio.to_thread(
                api.deployments.list_deployments, config.active_workspace_id
            )

            if not response.deployments:
                self._list_view._empty_message = "No deployments found"
                self._list_view.show_empty_message()
                return

            # Convert to list items
            items = []
            for dep in response.deployments:
                try:
                    # Extract status string safely
                    status_str = None
                    if hasattr(dep, "status") and dep.status:
                        if hasattr(dep.status, "status"):
                            status_str = dep.status.status
                        elif isinstance(dep.status, str):
                            status_str = dep.status

                    items.append(
                        ListItemData(
                            id=dep.id,
                            name=dep.name or "Unknown",
                            status=status_str,
                            data=dep,
                        )
                    )
                except Exception:
                    # Skip deployments that fail to parse
                    continue

            if not items:
                self._list_view._empty_message = "No valid deployments found"
                self._list_view.show_empty_message()
                return

            self._list_view.update_items(items)

            # Auto-select and highlight first deployment
            if items and self._list_view:
                self._list_view.index = 0
                self.selected_deployment_id = items[0].id

        except Exception as e:
            if self._list_view:
                self._list_view._empty_message = f"{str(e)}"
                self._list_view.show_empty_message()

        finally:
            if self._list_view:
                self._list_view.hide_loading()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Handle deployment selection"""
        self.selected_deployment_id = event.item.id

    def action_cursor_up(self) -> None:
        """Move cursor up in the list"""
        if self._list_view:
            self._list_view.action_cursor_up()

    def action_cursor_down(self) -> None:
        """Move cursor down in the list"""
        if self._list_view:
            self._list_view.action_cursor_down()


class UsageBreakdownTable(DataTable):
    """Table showing service-level usage breakdown"""

    deployment_usage: reactive[WorkspaceUsageResponse | None] = reactive(None)

    BINDINGS = [
        ("up,k", "cursor_up", "Move up"),
        ("down,j", "cursor_down", "Move down"),
    ]

    def __init__(self):
        super().__init__(
            zebra_stripes=True,
            id="usage-breakdown-table",
        )
        self.border_title = "📊 [2] Service Usage Breakdown"
        self.cursor_type = "row"
        self.show_cursor = True
        self.can_focus = True
        self.add_columns(
            "Name",
            "CPU (cs)",
            "Mem (GBs)",
        )
        self._usage_api = UsageAPI()

    def on_mount(self) -> None:
        """Show initial state"""
        self.show_row_labels = False
        self._show_message("Select a deployment to view usage")

    def on_focus(self) -> None:
        """Handle focus event"""
        self.border_subtitle = "↑↓/jk Navigate"

    def on_blur(self) -> None:
        """Handle blur event"""
        self.border_subtitle = ""

    def watch_deployment_usage(self, usage: WorkspaceUsageResponse | None) -> None:
        """Update table when deployment usage changes"""
        if not usage:
            return

        self.clear()

        if not usage.services:
            self._show_message("No usage data for last hour")
            return

        # Add service rows
        for idx, service in enumerate(usage.services):
            self.add_row(
                service.service_name,
                f"{service.cpu_core_seconds:.2f}",
                f"{service.memory_gb_seconds:.2f}",
                key=str(idx),
            )

        # Position cursor on first row
        if self.row_count > 0:
            self.move_cursor(row=0)

    def _show_message(self, message: str) -> None:
        """Show a message in the table"""
        self.clear()
        self.add_row(
            f"[dim]{message}[/dim]",
            "",
            "",
        )

    async def fetch_service_usage(self, deployment_id: str) -> None:
        """Fetch service usage for a deployment"""
        try:
            usage = await asyncio.to_thread(
                self._usage_api.get_usage,
                config.active_workspace_id,
                deployment_id=deployment_id,
            )
            self.deployment_usage = usage
        except Exception as e:
            self._show_message(f"{str(e)}")


class UsageMainContainer(Container):
    """Main container for usage dashboard"""

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
        """Watch for deployment selection changes"""
        if self._deployments_list:
            self.watch(
                self._deployments_list,
                "selected_deployment_id",
                self._on_deployment_selected,
            )

    def _on_deployment_selected(self, deployment_id: str | None) -> None:
        """Handle deployment selection change"""
        if deployment_id:
            # Update service breakdown
            if self._breakdown_table:
                self.run_worker(
                    self._breakdown_table.fetch_service_usage(deployment_id),
                    exclusive=True,
                )
            # Update volume usage
            if self._volume_table:
                self._volume_table.deployment_id = deployment_id


class UsageDashboard(App):
    """Main usage dashboard application"""

    CSS_PATH = "styles.tcss"
    ENABLE_COMMAND_PALETTE = False

    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
        ("escape", "quit", "Quit"),
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("1", "focus_deployments", "Deployments"),
        ("2", "focus_services", "Services"),
    ]

    def on_mount(self) -> None:
        """Setup the layout once the app is mounted"""
        # Register and activate theme
        self.register_theme(lazycloud_theme)
        self.theme = "lazycloud"

        # Set initial focus to deployments list
        try:
            deployments_list = self.query_one(DeploymentsListView)
            self.set_focus(deployments_list)
        except Exception:
            pass

    def compose(self) -> ComposeResult:
        """Compose the dashboard layout"""
        if not config.active_workspace_id:
            error_container = Container(id="error-container")
            error_container.border_title = "❌ Error"
            with error_container:
                error_text = dedent(f"""
                    [{Colors.Hex.error}]No active workspace.[/{Colors.Hex.error}]
                    
                    [{Colors.Hex.warning}]Use 'lazycloud workspace activate <name>' first.[/{Colors.Hex.warning}]
                """).strip()
                yield Static(error_text, id="error-message")
        else:
            yield UsageMainContainer()

    def action_refresh(self) -> None:
        """Refresh usage data"""
        try:
            overview = self.query_one(UsageOverviewSection, UsageOverviewSection)
            overview.refresh_usage()
        except Exception:
            pass

        try:
            sparkline = self.query_one(UsageTrendSparkline, UsageTrendSparkline)
            sparkline.refresh_trend()
        except Exception:
            pass

        try:
            deployments_list = self.query_one(DeploymentsListView, DeploymentsListView)
            deployments_list.run_worker(
                deployments_list._fetch_deployments_async(), exclusive=True
            )
        except Exception:
            pass

    def action_focus_deployments(self) -> None:
        """Focus the deployments list"""
        try:
            deployments_list = self.query_one(DeploymentsListView)
            self.set_focus(deployments_list)
        except Exception:
            pass

    def action_focus_services(self) -> None:
        """Focus the services table"""
        try:
            services_table = self.query_one(UsageBreakdownTable)
            self.set_focus(services_table)
        except Exception:
            pass

    def action_quit(self) -> None:
        """Quit the application"""
        self.exit()
