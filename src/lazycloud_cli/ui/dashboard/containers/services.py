from textual.app import ComposeResult
from textual.containers import Container

from lazycloud_cli.api import api
from lazycloud_cli.ui.dashboard.components import ListItemData, ListView
from lazycloud_cli.ui.dashboard.messages import ServiceSelected
from lazycloud_cli.ui.dashboard.theme import theme
from shared.models.statuses import ServiceStatus


class ServicesContainer(Container):
    """Container for displaying services."""

    BINDINGS = [
        ("up", "cursor_up", "Move up"),
        ("down", "cursor_down", "Move down"),
        ("enter", "select_item", "Select"),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.current_deployment_id = None
        self.current_deployment_name = None
        self._list_view = None
        self.border_title = "[2] Services"

    def compose(self) -> ComposeResult:
        """Create the services list"""
        self._list_view = ListView(
            on_select=self._handle_selection,
            id="services-list",
        )
        yield self._list_view

    def on_mount(self) -> None:
        """Style the container when mounted"""
        self.styles.height = theme.vertical_split
        self.styles.border = theme.get_border()
        self.styles.background = theme.background
        self.styles.padding = theme.padding
        self.can_focus = True
        # set empty message after mount
        if self._list_view:
            self._list_view._empty_message = "Select a deployment to view services"

    def on_focus(self) -> None:
        """Handle focus event."""
        self.styles.border = theme.get_border(focused=True)
        self.border_subtitle = "↑↓ Navigate • ↵ Select"
        # Reset other container borders
        if self.app:
            self.app.query(
                "#deployments-container"
            ).first().styles.border = theme.get_border()
            self.app.query("#main-container").first().styles.border = theme.get_border()

    def on_blur(self) -> None:
        """Handle blur event."""
        self.styles.border = theme.get_border()
        self.border_subtitle = None

    async def update_deployment(
        self, deployment_id: str, deployment_name: str = ""
    ) -> None:
        """Update the services list for a selected deployment."""
        if not self._list_view:
            return

        self.current_deployment_id = deployment_id
        self.current_deployment_name = deployment_name

        self._list_view.show_loading("Loading services...")
        try:
            status_response = api.deployments.get_deployment_status(deployment_id)

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
                            data=service,
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
            self.post_message(
                ServiceSelected(
                    self.current_deployment_id,
                    self.current_deployment_name,
                    item_data.data,
                )
            )

    def clear_services(self) -> None:
        """Clear the services list."""
        self.current_deployment_id = None
        self.current_deployment_name = None
        if self._list_view:
            self._list_view.update_items([])
            self._list_view.show_empty_message()

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
