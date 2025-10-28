import asyncio

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widgets import Static

from lazycloud_cli.api.usage import UsageAPI
from lazycloud_cli.config import config
from lazycloud_cli.ui.colors import Colors
from lazycloud_cli.ui.textual.components import Container
from lazycloud_cli.ui.textual.components.section import SectionContainer
from lazycloud_cli.ui.textual.theme import Icons
from shared.responses.usage import WorkspaceUsageResponse


class UsageOverviewSection(Container):
    """Section showing current billing period usage"""

    usage_data: reactive[WorkspaceUsageResponse | None] = reactive(None)

    def __init__(self):
        super().__init__(id="usage-overview-section")
        self._usage_api = UsageAPI()
        self._content_widget: Static | None = None
        self._section_container: SectionContainer | None = None

    def compose(self) -> ComposeResult:
        """Compose the usage overview"""
        self._section_container = SectionContainer(
            f"{Icons.COMPUTER} Current Billing Period"
        )
        with self._section_container:
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

    def _get_web_url(self) -> str:
        """Get web dashboard URL for detailed breakdown"""
        # Convert API URL to web URL (remove /api path if present)
        base_url = config.api_base_url.replace("/api", "")
        return f"{base_url}/usage"

    def watch_usage_data(self, usage: WorkspaceUsageResponse | None) -> None:
        """Update display when usage data changes"""
        if not usage or not self._content_widget or not self._section_container:
            return

        period_start = usage.period.start.strftime("%b %d")
        period_end = usage.period.end.strftime("%b %d, %Y")
        accent = Colors.Hex.accent

        # Update section title with date range using consistent format
        self._section_container.border_title = (
            f"{Icons.COMPUTER} [3] Current Billing • {period_start} - {period_end}"
        )

        # Get web URL for detailed breakdown
        web_url = self._get_web_url()

        # Display metrics on two rows with web link
        text = (
            f"[bold {accent}]CPU:[/bold {accent}] {usage.usage.cpu_core_hours:.2f} core-hrs  "
            f"[bold {accent}]Memory:[/bold {accent}] {usage.usage.memory_gb_hours:.2f} GB-hrs\n"
            f"[bold {accent}]Standard Storage:[/bold {accent}] {usage.usage.s3_gb_hours:.2f} GB-hrs  "
            f"[bold {accent}]Performance Storage:[/bold {accent}] {usage.usage.efs_gb_hours:.2f} GB-hrs\n"
            f"[dim]Visit {web_url} for detailed breakdown[/dim]"
        )

        self._content_widget.update(text)

    def refresh_usage(self) -> None:
        """Manually refresh usage data"""
        self.run_worker(self._fetch_usage_async(), exclusive=True)

