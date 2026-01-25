from models.statuses import DeploymentStatus, VolumeStatusSummary
from models.storage import StorageType
from textual.widgets import DataTable

from cli.ui.colors import Colors
from cli.ui.textual.dashboard.containers.details.utils import (
    get_status_color_from_string,
)


class VolumesTable(DataTable):
    """Non-interactive table displaying volume information."""

    def __init__(self, **kwargs):
        super().__init__(show_header=True, id="deployment-volumes-table", **kwargs)
        self.can_focus = False
        self.show_cursor = False
        self.zebra_stripes = True
        self._column_keys: list = []
        self._column_keys = self.add_columns("Name", "Status", "Type", "Size")

    def _build_row_data(self, volume: VolumeStatusSummary) -> tuple[str, str, str, str]:
        """Build the display values for a volume row."""
        color = get_status_color_from_string(volume.status)
        status_text = f"[{color}]{volume.status.upper()}[/{color}]"

        storage_text = (
            f"[{Colors.Hex.warning}]{volume.storage_type}[/{Colors.Hex.warning}]"
            if volume.storage_type == StorageType.SHARED
            else str(volume.storage_type)
        )

        size_text = volume.size or "-"

        return (volume.name, status_text, storage_text, size_text)

    def update_volumes(self, deployment: DeploymentStatus) -> None:
        """Update the table with volume data using delta updates to prevent flicker."""
        new_volumes = deployment.volumes or []
        current_row_count = self.row_count

        for idx, volume in enumerate(new_volumes):
            row_key = str(idx)
            new_values = self._build_row_data(volume)

            if idx < current_row_count:
                # Update existing row - only update cells that changed
                for col_idx, (col_key, new_value) in enumerate(
                    zip(self._column_keys, new_values)
                ):
                    current_value = self.get_cell(row_key, col_key)
                    if current_value != new_value:
                        self.update_cell(row_key, col_key, new_value)
            else:
                # Add new row
                self.add_row(*new_values, key=row_key)

        # Remove extra rows if volumes were removed
        while self.row_count > len(new_volumes):
            last_key = str(self.row_count - 1)
            self.remove_row(last_key)
