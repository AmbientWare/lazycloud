from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.reactive import reactive

from lazycloud_cli.ui.dashboard.components import Container, ListItemData, ListView
from lazycloud_cli.ui.dashboard.components.listview import ListItem
from lazycloud_cli.ui.dashboard.containers.content.container import ContentContainer
from lazycloud_cli.ui.dashboard.theme import theme
from shared.models.statuses import ServiceStatus

if TYPE_CHECKING:
    pass


class ServicesContainer(Container):
    """Container for displaying services."""

    # Reactive attributes
    services: reactive[list[ServiceStatus] | None] = reactive(None)
    selected_service: reactive[ServiceStatus | None] = reactive(None)

    BINDINGS = [
        ("up", "cursor_up", "Move up"),
        ("down", "cursor_down", "Move down"),
        ("enter", "select_item", "Select"),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._list_view = None
        self.border_title = "[2] Services"

    def compose(self) -> ComposeResult:
        """Create the services list"""
        self._list_view = ListView(
            on_select=self._handle_selection,
            on_highlight=self._handle_highlight,
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

        # Ensure an item is highlighted in the list
        if self._list_view:
            self._list_view.ensure_highlighted()
            # Only update if we're switching from deployment view or if service changed
            if self._list_view.index is not None and self._list_view.index < len(
                self._list_view.children
            ):
                item = self._list_view.children[self._list_view.index]
                if (
                    isinstance(item, ListItem)
                    and item.item_data.data
                    and isinstance(item.item_data.data, ServiceStatus)
                ):
                    if self.selected_service != item.item_data.data:
                        self.selected_service = item.item_data.data

    def on_blur(self) -> None:
        """Handle blur event."""
        self.styles.border = theme.get_border()
        self.border_subtitle = None

    async def watch_services(self, _old_value, new_value) -> None:
        """Auto-refresh when services list changes"""
        if new_value is not None and self._list_view:
            await self.refresh_service_list()

    async def watch_selected_service(self, _old_value, new_value) -> None:
        """React when a service is selected - update content"""
        if new_value:
            # Import here to avoid circular import
            content = self.app.query_one(ContentContainer)
            content.service = new_value

    async def refresh_service_list(self) -> None:
        """Update the services list for a selected deployment."""
        if not self._list_view:
            return

        self._list_view.show_loading("Loading services...")
        try:
            items = []
            if self.services:
                for service in self.services:
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
        """Handle service selection (Enter key pressed)."""
        if item_data.data and isinstance(item_data.data, ServiceStatus):
            self.selected_service = item_data.data

    def _handle_highlight(self, item_data: ListItemData) -> None:
        """Handle service highlight (arrow navigation)."""
        if item_data.data and isinstance(item_data.data, ServiceStatus):
            self.selected_service = item_data.data

    def clear_services(self) -> None:
        """Clear the services list."""
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
