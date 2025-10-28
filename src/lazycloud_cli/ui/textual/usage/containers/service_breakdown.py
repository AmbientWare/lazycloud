import asyncio
from datetime import datetime

from textual.reactive import reactive
from textual.widgets import DataTable

from lazycloud_cli.api.usage import UsageAPI
from lazycloud_cli.config import config
from lazycloud_cli.ui.textual.theme import Icons
from shared.responses.usage import WorkspaceUsageResponse


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
        self.border_title = f"{Icons.OVERVIEW} [2] Service Usage Breakdown"
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

    async def fetch_service_usage(
        self,
        deployment_id: str,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> None:
        """Fetch service usage for a deployment using the shared billing period"""
        try:
            usage = await asyncio.to_thread(
                self._usage_api.get_usage,
                config.active_workspace_id,
                start_date=start_date,
                end_date=end_date,
                deployment_id=deployment_id,
            )
            self.deployment_usage = usage
        except Exception as e:
            self._show_message(f"{str(e)}")

