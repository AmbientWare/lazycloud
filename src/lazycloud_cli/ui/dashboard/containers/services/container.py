"""
Services container for the dashboard.
"""

from textual.app import ComposeResult
from textual.containers import Container
from textual.message import Message
from textual.widgets import Label, ListItem, ListView

from lazycloud_cli.api import api
from lazycloud_cli.ui.dashboard.theme import theme
from shared.models.statuses import ServiceStatus


class ServiceItem(ListItem):
    """A service list item."""

    def __init__(
        self,
        service: ServiceStatus,
    ):
        super().__init__()
        self.service = service

    def compose(self) -> ComposeResult:
        """Create the service item layout."""
        # Status dot
        status_symbol = "●" if self.service.status == "running" else "○"
        dot = Label(status_symbol)
        dot.styles.width = 2
        dot.styles.color = theme.get_status_color(self.service.status)
        yield dot

        # Service name with ellipsis truncation
        name = Label(self.service.name)
        name.styles.width = "1fr"
        name.styles.overflow = "ellipsis"
        name.styles.text_overflow = "ellipsis"
        yield name

        # Replicas on the right
        replicas = Label(f"{self.service.ready_replicas}/{self.service.replicas}")
        replicas.styles.width = 8
        replicas.styles.text_align = "right"
        replicas.styles.color = theme.text_dim
        yield replicas

    def on_mount(self) -> None:
        """Apply minimal styling."""
        self.styles.layout = "horizontal"
        self.styles.height = 1
        self.styles.padding = (0, 1)


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

    def compose(self) -> ComposeResult:
        """Create the services list."""
        list_view = ListView(id="services-list")
        list_view.styles.background = theme.background
        yield list_view

    def on_mount(self) -> None:
        """Style the container when mounted."""
        self.border_title = "Services [2]"
        self.styles.height = theme.vertical_split
        self.styles.border = theme.get_border()
        self.styles.background = theme.background
        self.styles.padding = theme.padding

        # Show initial message
        self.show_no_deployment_message()

    def show_no_deployment_message(self) -> None:
        """Show message when no deployment is selected."""
        list_view = self.query_one("#services-list", ListView)
        list_view.clear()
        list_view.append(ListItem(Label("Select a deployment to view services")))

    async def update_deployment(self, deployment_id: str, deployment_name: str) -> None:
        """Update the services list for a selected deployment."""
        self.current_deployment_id = deployment_id
        self.current_deployment_name = deployment_name
        self.border_title = f"Services [2] - {deployment_name}"

        # Clear and show loading
        list_view = self.query_one("#services-list", ListView)
        list_view.clear()
        list_view.loading = True

        try:
            # Fetch deployment status which includes services
            status_response = api.deployments.get_deployment_status(deployment_id)

            if not status_response.status.services:
                list_view.append(ListItem(Label("No services found")))
            else:
                # Add services to the list
                for service in status_response.status.services:
                    item = ServiceItem(service=service)
                    list_view.append(item)

        except Exception as e:
            list_view.append(ListItem(Label(f"Error loading services: {str(e)}")))

        finally:
            list_view.loading = False

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Handle service selection."""
        if isinstance(event.item, ServiceItem):
            # Emit message for parent to handle
            self.post_message(
                self.ServiceSelected(
                    self.current_deployment_id,
                    self.current_deployment_name,
                    event.item.service,
                )
            )

    def clear_services(self) -> None:
        """Clear the services list."""
        self.current_deployment_id = None
        self.current_deployment_name = None
        self.border_title = "Services [2]"
        self.show_no_deployment_message()
