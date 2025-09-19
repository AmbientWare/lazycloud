from textual.app import ComposeResult
from textual.reactive import reactive

from lazycloud_cli.api import api
from lazycloud_cli.ui.dashboard.components import Container, ListItemData, ListView
from lazycloud_cli.ui.dashboard.components.listview import ListItem
from lazycloud_cli.ui.dashboard.containers.content.container import ContentContainer
from lazycloud_cli.ui.dashboard.containers.services import ServicesContainer
from lazycloud_cli.ui.dashboard.theme import theme
from shared.models.statuses import DeploymentStatus, ServiceStatus
from shared.responses.deployments import DeploymentResponse


class DeploymentsContainer(Container):
    """Container for displaying and selecting deployments."""

    # Reactive attributes
    selected_deployment: reactive[DeploymentResponse | None] = reactive(None)
    deployment_status: reactive[DeploymentStatus | None] = reactive(None)
    services: reactive[list[ServiceStatus] | None] = reactive(None)

    BINDINGS = [
        ("up", "cursor_up", "Move up"),
        ("down", "cursor_down", "Move down"),
        ("enter", "select_item", "Select"),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._list_view = None
        self.border_title = "[1] Deployments"

    def compose(self) -> ComposeResult:
        """Create the deployments widget."""
        self._list_view = ListView(
            on_select=self._handle_selection,
            on_highlight=self._handle_highlight,
            empty_message="No deployments found",
            id="deployments-list",
        )
        yield self._list_view

    async def on_mount(self) -> None:
        """Style the container when mounted and load deployments."""
        self.styles.height = theme.vertical_split
        self.styles.border = theme.get_border()
        self.styles.background = theme.background
        self.styles.padding = theme.padding
        self.can_focus = True

        # Load deployments after mounting
        await self.load_deployments()

    def on_focus(self) -> None:
        """Handle focus event."""
        self.styles.border = theme.get_border(focused=True)
        self.border_subtitle = "↑↓ Navigate • ↵ Select"

        # Ensure an item is highlighted in the list
        if self._list_view:
            self._list_view.ensure_highlighted()
            # Only update if we're switching from service view or if deployment changed
            if self._list_view.index is not None and self._list_view.index < len(
                self._list_view.children
            ):
                item = self._list_view.children[self._list_view.index]
                if isinstance(item, ListItem):
                    # Only fetch and update if it's a different deployment
                    if self.selected_deployment != item.item_data.data:
                        self.selected_deployment = item.item_data.data
                        # get status associated with the deployment
                        status = api.deployments.get_deployment_status(
                            item.item_data.id
                        )
                        self.deployment_status = status.status
                        self.services = status.status.services

    def on_blur(self) -> None:
        """Handle blur event."""
        self.styles.border = theme.get_border()
        self.border_subtitle = None

    async def watch_selected_deployment(self, old_value, new_value) -> None:
        """React when deployment is selected - update content container"""
        if new_value:
            # Update the content container
            content = self.app.query_one(ContentContainer)
            content.deployment = new_value
            content.deployment_status = self.deployment_status

    async def watch_services(self, old_value, new_value) -> None:
        """React when services list changes - update services container"""
        if new_value is not None:
            # Update the services container
            services = self.app.query_one(ServicesContainer)
            services.services = new_value

    async def load_deployments(self) -> None:
        """Fetch and display deployments from API."""
        if not self._list_view:
            return

        self._list_view.show_loading("Loading deployments...")

        try:
            response = api.deployments.list_deployments()

            items = []
            if response.deployments:
                for deployment in response.deployments:
                    items.append(
                        ListItemData(
                            id=deployment.id,
                            name=deployment.name,
                            status=deployment.state,
                            data=deployment,
                        )
                    )

            self._list_view.update_items(items)

        except Exception:
            self._list_view.update_items([])
            self._list_view.show_empty_message()

        finally:
            self._list_view.hide_loading()

    def _handle_selection(self, item_data: ListItemData) -> None:
        """Handle deployment selection (Enter key pressed)."""
        # Update our own reactive state
        self.selected_deployment = item_data.data
        # get status associated with the deployment
        status = api.deployments.get_deployment_status(item_data.id)
        self.deployment_status = status.status
        # update the services
        self.services = status.status.services

    def _handle_highlight(self, item_data: ListItemData) -> None:
        """Handle deployment highlight (arrow navigation)."""
        # Only update content if the highlight actually changed to a different item
        if self.selected_deployment != item_data.data:
            self.selected_deployment = item_data.data
            # get status associated with the deployment
            status = api.deployments.get_deployment_status(item_data.id)
            self.deployment_status = status.status
            # update the services
            self.services = status.status.services

    def action_cursor_up(self) -> None:
        """Move cursor up in the list."""
        if self._list_view:
            self._list_view.action_cursor_up()

    def action_cursor_down(self) -> None:
        """Move cursor down in the list."""
        if self._list_view:
            self._list_view.action_cursor_down()

    def action_select_item(self) -> None:
        """Select the currently highlighted item."""
        if self._list_view:
            self._list_view.action_select_cursor()
