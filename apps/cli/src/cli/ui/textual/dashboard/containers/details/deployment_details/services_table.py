from models.statuses import DeploymentStatus, ServiceStatus
from textual.widgets import DataTable

from cli.ui.colors import Colors
from cli.ui.textual.dashboard.containers.details.utils import get_status_color


class ServicesTable(DataTable):
    """Non-interactive table displaying service information."""

    def __init__(self, **kwargs):
        super().__init__(show_header=True, id="deployment-services-table", **kwargs)
        self.can_focus = False
        self.show_cursor = False
        self.zebra_stripes = True
        self._column_keys: list = []
        self._column_keys = self.add_columns("Name", "Status", "Replicas", "Restarts")

    def _build_row_data(self, service: ServiceStatus) -> tuple[str, str, str, str]:
        """Build the display values for a service row."""
        color = get_status_color(service.status)
        status_text = f"[{color}]{service.status.upper()}[/{color}]"

        restart_text = (
            f"[{Colors.Hex.error}]{service.restarts}[/{Colors.Hex.error}]"
            if service.restarts > 0
            else str(service.restarts)
        )

        return (
            service.name,
            status_text,
            f"{service.ready_replicas}/{service.total_replicas}",
            restart_text,
        )

    def update_services(self, deployment: DeploymentStatus) -> None:
        """Update the table with service data using delta updates to prevent flicker."""
        new_services = deployment.services or []
        current_row_count = self.row_count

        for idx, service in enumerate(new_services):
            row_key = str(idx)
            new_values = self._build_row_data(service)

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

        # Remove extra rows if services were removed
        while self.row_count > len(new_services):
            last_key = str(self.row_count - 1)
            self.remove_row(last_key)
