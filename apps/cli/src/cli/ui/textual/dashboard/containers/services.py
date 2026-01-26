from models.statuses import ServiceStatus
from textual.app import ComposeResult
from textual.reactive import reactive

from cli.api import api
from cli.ui.textual.components import Container, ListItemData, ListView
from cli.ui.textual.components.listview import ListItem
from cli.ui.textual.messages import ServiceSelected
from cli.ui.textual.theme import Icons


class ServicesContainer(Container):
    deployment_id: reactive[str | None] = reactive(None)
    services: reactive[list[ServiceStatus] | None] = reactive(None)
    selected_service: reactive[ServiceStatus | None] = reactive(None)

    BINDINGS = [
        ("up,k", "cursor_up", "Move up"),
        ("down,j", "cursor_down", "Move down"),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._list_view = None
        self.border_title = f"{Icons.WRENCH} [2] Services"

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
        self.can_focus = True
        # set empty message after mount
        if self._list_view:
            self._list_view._empty_message = "Select a deployment to view services"

    async def on_focus(self) -> None:
        """Handle focus event."""
        self.border_subtitle = "↑↓/jk Navigate"

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
                    # Only fetch and update if it's a different service
                    if self.selected_service != item.item_data.data:
                        self.selected_service = item.item_data.data
                        # Fetch fresh status from API
                        try:
                            status = await api.services.get_service_status(
                                self.deployment_id, item.item_data.data.name
                            )
                            self.selected_service = status.service
                        except Exception as e:
                            self.log.error(f"Failed to get service status: {e}")

    def on_blur(self) -> None:
        """Handle blur event."""
        self.border_subtitle = None

    def on_click(self) -> None:
        """Handle mouse clicks - switch to services view."""
        self.app.action_switch_to_services()

    async def watch_deployment_id(self, _old_value, new_value) -> None:
        """Auto-refresh when services list changes"""
        if new_value is not None and self._list_view:
            await self.refresh_services()

    async def watch_selected_service(self, _old_value, new_value) -> None:
        """React when a service is selected - post message for other components"""
        if new_value and self.deployment_id:
            # Post message instead of directly updating other containers
            self.post_message(
                ServiceSelected(
                    deployment_id=self.deployment_id,
                    service_status=new_value,
                )
            )

    async def refresh_services(self) -> None:
        """Update the services list for a selected deployment."""
        if not self._list_view:
            return

        self._list_view.show_loading("Loading services...")
        try:
            service_statuses = await api.services.get_service_statuses(
                self.deployment_id
            )

            items = []
            services = []
            if service_statuses:
                for status in service_statuses:
                    services.append(status.service)
                    items.append(
                        ListItemData(
                            id=status.service.name,
                            name=status.service.name,
                            data=status.service,
                        )
                    )

            self._list_view.update_items(items)
            self.services = services

        except Exception as e:
            self.log.error(f"Failed to load services: {e}")
            self._list_view.update_items([])
            self._list_view.show_empty_message()

        finally:
            self._list_view.hide_loading()

    async def _handle_selection(self, item_data: ListItemData) -> None:
        """Handle service selection (Enter key pressed)."""
        if item_data.data and isinstance(item_data.data, ServiceStatus):
            # Set immediately for instant UI feedback (like deployments pattern)
            self.selected_service = item_data.data
            # Then fetch fresh status from API
            try:
                status = await api.services.get_service_status(
                    self.deployment_id, item_data.data.name
                )
                self.selected_service = status.service
            except Exception as e:
                self.log.error(f"Failed to get service status: {e}")

    def _handle_highlight(self, item_data: ListItemData) -> None:
        """Handle service highlight with api request debouncing."""
        self._selection_timer = self.handle_debounce(
            self._selection_timer,
            lambda: self.call_later(self._update_selected_service, item_data),
        )

    async def _update_selected_service(self, item_data: ListItemData) -> None:
        """Fetch service status after debounce delay."""
        self._selection_timer = None

        if item_data.data and isinstance(item_data.data, ServiceStatus):
            # Only update content if the highlight actually changed to a different item
            if self.selected_service != item_data.data:
                # Set immediately for instant UI feedback (like deployments pattern)
                self.selected_service = item_data.data
                # Then fetch fresh status from API
                try:
                    status = await api.services.get_service_status(
                        self.deployment_id, item_data.data.name
                    )
                    self.selected_service = status.service
                except Exception as e:
                    self.log.error(f"Failed to get service status: {e}")

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
