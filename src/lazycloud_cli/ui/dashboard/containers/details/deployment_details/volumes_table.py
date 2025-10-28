from textual.widgets import DataTable

from lazycloud_cli.ui.colors import Colors
from lazycloud_cli.ui.dashboard.containers.details.utils import (
    get_status_color_from_string,
)
from shared.models.statuses import DeploymentStatus, StorageType


class VolumesTable(DataTable):
    """Non-interactive table displaying volume information."""

    def __init__(self, **kwargs):
        super().__init__(show_header=True, id="deployment-volumes-table", **kwargs)
        self.can_focus = False
        self.show_cursor = False
        self.zebra_stripes = True
        self.add_columns("Name", "Status", "Type")

    def update_volumes(self, deployment: DeploymentStatus) -> None:
        """Update the table with volume data."""
        self.clear()
        for idx, volume in enumerate(deployment.volumes):
            # Use shared utility for status color
            color = get_status_color_from_string(volume.status)
            status_text = f"[{color}]{volume.status.upper()}[/{color}]"

            # Highlight high performance storage
            storage_text = (
                f"[{Colors.Hex.warning}]{volume.storage_type}[/{Colors.Hex.warning}]"
                if volume.storage_type == StorageType.PREMIUM
                else str(volume.storage_type)
            )

            self.add_row(
                volume.name,
                status_text,
                storage_text,
                key=str(idx),
            )
