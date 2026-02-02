from models.statuses import PodStatus
from textual.widgets import DataTable

from cli.ui.textual.dashboard.containers.details.service_details.delete_instance_modal import (
    DeleteInstanceModal,
)
from cli.ui.textual.dashboard.containers.details.service_details.logs_modal import (
    LogViewerModal,
)
from cli.ui.textual.dashboard.containers.details.utils import get_status_color
from cli.ui.textual.theme import Icons
from cli.utils.utils import format_cpu, format_memory


class PodTable(DataTable):
    """Custom DataTable for pod selection that handles its own events."""

    BINDINGS = [
        ("d", "delete_instance", "Delete Instance"),
        ("f", "force_delete_instance", "Force Delete Instance"),
        ("l", "show_logs", "Show Logs"),
        ("j,down", "cursor_down", "Move down"),
        ("k,up", "cursor_up", "Move up"),
    ]

    def __init__(self, deployment_id: str, service_name: str, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.deployment_id = deployment_id
        self.service_name = service_name
        self.selected_pod_name = None
        self.user_has_interacted = False
        self.can_focus = True
        self.cursor_type = "row"
        self.show_cursor = True
        self._pods: list[PodStatus] = []
        self._pod_keys: set[str] = set()  # Track current pod keys for delta updates
        self.border_title = f"{Icons.COMPUTER} [5] Instances"

        self._column_keys: list = []
        self._column_keys = self.add_columns(
            "Instance Name",
            "Status",
            "Ready",
            "CPU",
            "Memory",
            "Restarts",
            "Age",
        )

    def on_mount(self) -> None:
        """Initialize the table on mount."""
        self.styles.height = "auto"
        self.styles.max_height = "50%"
        self.zebra_stripes = False
        self.show_row_labels = False

    def action_delete_instance(self) -> None:
        """Handle the delete instance action."""
        # exit for logging purposes
        if (
            not self.deployment_id
            or not self.service_name
            or not self.selected_pod_name
        ):
            return

        modal = DeleteInstanceModal(
            service_name=self.service_name,
            deployment_id=self.deployment_id,
            pod_name=self.selected_pod_name,
            force=False,
        )
        self.app.push_screen(modal)

    def action_force_delete_instance(self) -> None:
        """Handle the force delete instance action."""
        if (
            not self.deployment_id
            or not self.service_name
            or not self.selected_pod_name
        ):
            return

        modal = DeleteInstanceModal(
            service_name=self.service_name,
            deployment_id=self.deployment_id,
            pod_name=self.selected_pod_name,
            force=True,
        )
        self.app.push_screen(modal)

    def action_show_logs(self) -> None:
        """Handle the show logs action."""
        if self.selected_pod_name:
            modal = LogViewerModal(
                deployment_id=self.deployment_id,
                service_name=self.service_name,
                pod_name=self.selected_pod_name,
            )
            self.app.push_screen(modal)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle row selection and open log modal."""
        if event.row_key and event.row_key.value:
            pod_name = event.row_key.value
            self.user_has_interacted = True
            modal = LogViewerModal(
                deployment_id=self.deployment_id,
                service_name=self.service_name,
                pod_name=pod_name,
            )
            self.app.push_screen(modal)

    def on_focus(self) -> None:
        """Handle focus event."""
        self.border_subtitle = "↑↓/jk Navigate • l: Logs • d: Delete • f: Force Delete"

    def on_blur(self) -> None:
        """Handle blur event."""
        self.border_subtitle = ""

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Handle row highlight (cursor movement)."""
        self.user_has_interacted = True
        if event.row_key:
            self.selected_pod_name = event.row_key.value

    def _build_row_data(
        self, pod: PodStatus
    ) -> tuple[str, str, str, str, str, str, str]:
        """Build the display values for a pod row."""
        status_color = get_status_color(pod.phase)
        # Include error reason in status if available
        if pod.reason and pod.phase.value in ["Error", "Pending"]:
            status_text = (
                f"[{status_color}]{pod.phase.value}: {pod.reason}[/{status_color}]"
            )
        else:
            status_text = f"[{status_color}]{pod.phase.value}[/{status_color}]"

        ready = f"{pod.ready_containers}/{pod.total_containers}"

        # Format CPU and memory using Docker Compose style
        if pod.cpu_usage and pod.cpu_usage != "N/A":
            cpu = f"{format_cpu(pod.cpu_usage)} cores"
        else:
            cpu = "N/A"

        if pod.memory_usage and pod.memory_usage != "N/A":
            memory = format_memory(pod.memory_usage)
        else:
            memory = "N/A"

        restarts = str(pod.restart_count) if pod.restart_count > 0 else "0"
        age = pod.age or "Unknown"

        display_name = pod.name
        if len(display_name) > 30:
            display_name = display_name[:27] + "..."

        return (display_name, status_text, ready, cpu, memory, restarts, age)

    def update_pods(self, pods: list[PodStatus]) -> None:
        """Update the table with new pod data using delta updates to prevent flicker."""
        self._pods = pods
        new_pod_keys = {pod.name for pod in pods}

        # Remove pods that no longer exist
        pods_to_remove = self._pod_keys - new_pod_keys
        for pod_key in pods_to_remove:
            try:
                self.remove_row(pod_key)
            except Exception:
                pass

        # Update existing pods and add new ones
        for pod in pods:
            new_values = self._build_row_data(pod)

            if pod.name in self._pod_keys:
                # Update existing row - only update cells that changed
                for col_key, new_value in zip(self._column_keys, new_values):
                    try:
                        current_value = self.get_cell(pod.name, col_key)
                        if current_value != new_value:
                            self.update_cell(pod.name, col_key, new_value)
                    except Exception:
                        pass
            else:
                # Add new row
                self.add_row(*new_values, key=pod.name)

        self._pod_keys = new_pod_keys
