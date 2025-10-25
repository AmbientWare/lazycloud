from textual.widgets import DataTable

from lazycloud_cli.ui.colors import Colors
from lazycloud_cli.ui.dashboard.containers.details.utils import get_status_color
from shared.models.statuses import DeploymentStatus


class ServicesTable(DataTable):
    """Non-interactive table displaying service information."""

    def __init__(self, **kwargs):
        super().__init__(show_header=True, id="deployment-services-table", **kwargs)
        self.can_focus = False
        self.show_cursor = False
        self.zebra_stripes = True
        self.add_columns("Name", "Status", "Replicas", "Restarts")

    def update_services(self, deployment: DeploymentStatus) -> None:
        """Update the table with service data."""
        self.clear()
        for idx, service in enumerate(deployment.services):
            # Use shared utility for status color
            color = get_status_color(service.status)
            status_text = f"[{color}]{service.status.upper()}[/{color}]"

            # Highlight restarts if > 0
            restart_text = (
                f"[{Colors.Hex.error}]{service.restarts}[/{Colors.Hex.error}]"
                if service.restarts > 0
                else str(service.restarts)
            )

            self.add_row(
                service.name,
                status_text,
                f"{service.ready_replicas}/{service.total_replicas}",
                restart_text,
                key=str(idx),
            )
