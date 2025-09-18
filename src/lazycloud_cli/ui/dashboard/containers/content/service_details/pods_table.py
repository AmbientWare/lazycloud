from textual.widgets import DataTable

from lazycloud_cli.ui.dashboard.containers.content.service_details.logs_modal import (
    LogViewerModal,
)


class PodTable(DataTable):
    """Custom DataTable for pod selection that handles its own events."""

    def __init__(self, service_view, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.service_view = service_view
        self.user_has_interacted = False

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle row selection and open log modal."""
        if event.row_key:
            pod_name = event.row_key.value
            self.user_has_interacted = True
            if (
                self.service_view.current_deployment_id
                and self.service_view.current_service_name
            ):
                modal = LogViewerModal(
                    deployment_id=self.service_view.current_deployment_id,
                    service_name=self.service_view.current_service_name,
                    pod_name=pod_name,
                )
                self.app.push_screen(modal)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Handle row highlight (cursor movement)."""
        # Mark as interacted when user navigates
        self.user_has_interacted = True
