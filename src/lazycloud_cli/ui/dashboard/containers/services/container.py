from textual.app import ComposeResult
from textual.containers import Container
from textual.message import Message

from lazycloud_cli.api import api
from lazycloud_cli.ui.dashboard.components import ListItemData, ListView
from lazycloud_cli.ui.dashboard.theme import theme
from shared.models.statuses import ServiceStatus


class ServicesContainer(Container):
    """Container for displaying services."""

    class ServiceSelected(Message):
        """Message emitted when a service is selected."""

        def __init__(
            self,
            deployment_id: str,
            deployment_name: str,
            service: ServiceStatus,
        ):
            super().__init__()
            self.deployment_id = deployment_id
            self.deployment_name = deployment_name
            self.service = service

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.current_deployment_id = None
        self.current_deployment_name = None
        self._list_view = None

    def compose(self) -> ComposeResult:
        """Create the services list."""
        self._list_view = ListView(
            on_select=self._handle_selection,
            empty_message="Select a deployment to view services",
            id="services-list",
        )
        yield self._list_view

    def on_mount(self) -> None:
        """Style the container when mounted."""
        self.border_title = "Services [2]"
        self.styles.height = theme.vertical_split
        self.styles.border = theme.get_border()
        self.styles.background = theme.background
        self.styles.padding = theme.padding

    async def update_deployment(
        self, deployment_id: str, deployment_name: str = ""
    ) -> None:
        """Update the services list for a selected deployment."""
        if not self._list_view:
            return

        self.current_deployment_id = deployment_id
        self.current_deployment_name = deployment_name
        self.border_title = "Services [2]"

        # Show loading
        self._list_view.show_loading("Loading services...")

        try:
            # Fetch deployment status which includes services
            status_response = api.deployments.get_deployment_status(deployment_id)

            # Convert services to ListItemData
            items = []
            if status_response.status.services:
                for service in status_response.status.services:
                    extra_text = f"{service.ready_replicas}/{service.replicas}"
                    items.append(
                        ListItemData(
                            id=service.name,
                            name=service.name,
                            status=service.status,
                            extra_text=extra_text,
                            data=service,  # Store the full service object
                        )
                    )

            self._list_view.update_items(items)

        except Exception:
            self._list_view.update_items([])
            self._list_view.show_empty_message()

        finally:
            self._list_view.hide_loading()

    def _handle_selection(self, item_data: ListItemData) -> None:
        """Handle service selection."""
        if item_data.data and isinstance(item_data.data, ServiceStatus):
            # Emit message for parent to handle
            self.post_message(
                self.ServiceSelected(
                    self.current_deployment_id,
                    self.current_deployment_name,
                    item_data.data,
                )
            )

    def clear_services(self) -> None:
        """Clear the services list."""
        self.current_deployment_id = None
        self.current_deployment_name = None
        self.border_title = "Services [2]"
        if self._list_view:
            self._list_view.update_items([])
            self._list_view.show_empty_message()
