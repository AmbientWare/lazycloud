from models.statuses import DeploymentStatus, NetworkStatus
from textual.widgets import DataTable

from cli.ui.textual.dashboard.containers.details.utils import (
    get_status_color_from_string,
)


class NetworksTable(DataTable):
    """Non-interactive table displaying network information."""

    def __init__(self, **kwargs):
        super().__init__(show_header=True, id="deployment-networks-table", **kwargs)
        self.can_focus = False
        self.show_cursor = False
        self.zebra_stripes = True
        self._column_keys: list = []
        self._column_keys = self.add_columns("Name", "Status")

    def _build_row_data(self, network: NetworkStatus) -> tuple[str, str]:
        """Build the display values for a network row."""
        color = get_status_color_from_string(network.status)
        status_text = f"[{color}]{network.status.upper()}[/{color}]"
        return (network.name, status_text)

    def update_networks(self, deployment: DeploymentStatus) -> None:
        """Update the table with network data using delta updates to prevent flicker."""
        new_networks = deployment.networks or []
        current_row_count = self.row_count

        for idx, network in enumerate(new_networks):
            row_key = str(idx)
            new_values = self._build_row_data(network)

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

        # Remove extra rows if networks were removed
        while self.row_count > len(new_networks):
            last_key = str(self.row_count - 1)
            self.remove_row(last_key)
