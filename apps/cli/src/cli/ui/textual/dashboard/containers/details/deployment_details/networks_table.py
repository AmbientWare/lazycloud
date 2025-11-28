from models.statuses import DeploymentStatus
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
        self.add_columns("Name", "Status")

    def update_networks(self, deployment: DeploymentStatus) -> None:
        """Update the table with network data."""
        self.clear()
        for idx, network in enumerate(deployment.networks):
            # Use shared utility for status color
            color = get_status_color_from_string(network.status)
            status_text = f"[{color}]{network.status.upper()}[/{color}]"

            self.add_row(
                network.name,
                status_text,
                key=str(idx),
            )
