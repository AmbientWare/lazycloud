import asyncio
from datetime import datetime

from textual.reactive import reactive
from textual.widgets import DataTable

from lazycloud_cli.api.usage import UsageAPI
from lazycloud_cli.config import config
from lazycloud_cli.ui.textual.theme import Icons
from shared.models.billing import STORAGE_CLASS_TO_TYPE


class VolumeUsageTable(DataTable):
    """Table showing volume usage breakdown"""

    deployment_id: reactive[str | None] = reactive(None)
    period_start: reactive[datetime | None] = reactive(None)
    period_end: reactive[datetime | None] = reactive(None)

    BINDINGS = [
        ("up,k", "cursor_up", "Move up"),
        ("down,j", "cursor_down", "Move down"),
    ]

    def __init__(self):
        super().__init__(
            zebra_stripes=True,
            id="volume-usage-table",
        )
        self.border_title = f"{Icons.SAVE} Storage Usage"
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
        """Fetch volume usage for a deployment using the shared billing period"""
        try:
            # Get usage data with deployment_id for detailed breakdown
            usage = await asyncio.to_thread(
                self._usage_api.get_usage,
                config.active_workspace_id,
                start_date=self.period_start,
                end_date=self.period_end,
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

