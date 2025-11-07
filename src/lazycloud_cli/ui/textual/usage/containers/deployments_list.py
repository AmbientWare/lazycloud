from textual.app import ComposeResult
from textual.reactive import reactive

from lazycloud_cli.ui.colors import Colors
from lazycloud_cli.ui.textual.components import Container
from lazycloud_cli.ui.textual.components.listview import (
    ListItem,
    ListItemData,
    ListView,
)
from lazycloud_cli.ui.textual.theme import Icons
from shared.responses.usage import (
    WorkspaceUsageWithDeploymentsResponse,
)


class ActiveDeploymentsContainer(Container):
    """Container for active deployments list"""

    selected_deployment_id: reactive[str | None] = reactive(None)
    usage_data: reactive[WorkspaceUsageWithDeploymentsResponse | None] = reactive(None)

    BINDINGS = [
        ("up,k", "cursor_up", "Move up"),
        ("down,j", "cursor_down", "Move down"),
    ]

    def __init__(self):
        super().__init__(id="active-deployments-container")
        self._list_view: ListView | None = None
        self.border_title = f"[1] [bold {Colors.Hex.success}]Active[/bold {Colors.Hex.success}]"

    def compose(self) -> ComposeResult:
        """Compose the active deployments list"""
        self._list_view = ListView(
            id="active-deployments-list",
            on_select=self._handle_selection,
            on_highlight=self._handle_highlight,
        )
        yield self._list_view

    def on_mount(self) -> None:
        """Set up when mounted"""
        self.can_focus = True
        self.border_subtitle = "↑↓/jk Navigate"
        if self._list_view:
            self._list_view.show_loading("Loading deployments...")

    def on_focus(self) -> None:
        """Handle focus event"""
        if self._list_view:
            self._list_view.ensure_highlighted()
            # Update selected_deployment_id if list view has a highlighted item
            if self._list_view.index is not None and self._list_view.index < len(
                self._list_view.children
            ):
                item = self._list_view.children[self._list_view.index]
                if isinstance(item, ListItem) and item.item_data:
                    # Always update to ensure breakdown is refreshed
                    self.selected_deployment_id = item.item_data.id
            elif len(self._list_view.children) > 0:
                # If no index set but we have items, select first one
                self._list_view.index = 0
                first_item = self._list_view.children[0]
                if isinstance(first_item, ListItem) and first_item.item_data:
                    self.selected_deployment_id = first_item.item_data.id

    def watch_usage_data(
        self, usage_data: WorkspaceUsageWithDeploymentsResponse | None
    ) -> None:
        """Update deployments list when usage data is loaded"""
        if not self._list_view:
            return

        if not usage_data or not usage_data.deployments:
            self._list_view._empty_message = "No active deployments found"
            self._list_view.show_empty_message()
            self._list_view.hide_loading()
            return

        # Filter active deployments
        active_deployments = [
            d for d in usage_data.deployments if d.status == "Active"
        ]

        # Convert to list items
        active_items = []
        for deployment in active_deployments:
            try:
                active_items.append(
                    ListItemData(
                        id=deployment.deployment_id,
                        name=deployment.deployment_name or "Unknown",
                        status=None,
                        data=deployment,
                    )
                )
            except Exception:
                continue

        # Update list
        if active_items:
            self._list_view.update_items(active_items)
            # Auto-select first if none selected
            if not self.selected_deployment_id:
                self.selected_deployment_id = active_items[0].id
                self.call_after_refresh(lambda: setattr(self._list_view, "index", 0))
            else:
                # Try to find selected deployment
                try:
                    current_index = next(
                        (idx for idx, item in enumerate(active_items) if item.id == self.selected_deployment_id),
                        None,
                    )
                    if current_index is not None:
                        self.call_after_refresh(
                            lambda idx=current_index: setattr(self._list_view, "index", idx)
                        )
                except StopIteration:
                    pass
        else:
            self._list_view._empty_message = "No active deployments"
            self._list_view.show_empty_message()
        self._list_view.hide_loading()

    def _handle_selection(self, item_data: ListItemData) -> None:
        """Handle deployment selection (Enter key pressed)"""
        self.selected_deployment_id = item_data.id

    def _handle_highlight(self, item_data: ListItemData) -> None:
        """Handle deployment highlight (arrow navigation)"""
        self.selected_deployment_id = item_data.id

    def action_cursor_up(self) -> None:
        """Move cursor up"""
        if self._list_view:
            self._list_view.action_cursor_up()

    def action_cursor_down(self) -> None:
        """Move cursor down"""
        if self._list_view:
            self._list_view.action_cursor_down()


class InactiveDeploymentsContainer(Container):
    """Container for inactive deployments list"""

    selected_deployment_id: reactive[str | None] = reactive(None)
    usage_data: reactive[WorkspaceUsageWithDeploymentsResponse | None] = reactive(None)

    BINDINGS = [
        ("up,k", "cursor_up", "Move up"),
        ("down,j", "cursor_down", "Move down"),
    ]

    def __init__(self):
        super().__init__(id="inactive-deployments-container")
        self._list_view: ListView | None = None
        self.border_title = f"[2] [bold {Colors.Hex.warning}]Inactive[/bold {Colors.Hex.warning}]"

    def compose(self) -> ComposeResult:
        """Compose the inactive deployments list"""
        self._list_view = ListView(
            id="inactive-deployments-list",
            on_select=self._handle_selection,
            on_highlight=self._handle_highlight,
        )
        yield self._list_view

    def on_mount(self) -> None:
        """Set up when mounted"""
        self.can_focus = True
        self.border_subtitle = "↑↓/jk Navigate"
        if self._list_view:
            self._list_view.show_loading("Loading deployments...")

    def on_focus(self) -> None:
        """Handle focus event"""
        if self._list_view:
            self._list_view.ensure_highlighted()
            # Update selected_deployment_id if list view has a highlighted item
            if self._list_view.index is not None and self._list_view.index < len(
                self._list_view.children
            ):
                item = self._list_view.children[self._list_view.index]
                if isinstance(item, ListItem) and item.item_data:
                    # Always update to ensure breakdown is refreshed
                    self.selected_deployment_id = item.item_data.id
            elif len(self._list_view.children) > 0:
                # If no index set but we have items, select first one
                self._list_view.index = 0
                first_item = self._list_view.children[0]
                if isinstance(first_item, ListItem) and first_item.item_data:
                    self.selected_deployment_id = first_item.item_data.id

    def watch_usage_data(
        self, usage_data: WorkspaceUsageWithDeploymentsResponse | None
    ) -> None:
        """Update deployments list when usage data is loaded"""
        if not self._list_view:
            return

        if not usage_data or not usage_data.deployments:
            self._list_view._empty_message = "No inactive deployments found"
            self._list_view.show_empty_message()
            self._list_view.hide_loading()
            return

        # Filter inactive deployments
        inactive_deployments = [
            d for d in usage_data.deployments if d.status == "Inactive"
        ]

        # Convert to list items
        inactive_items = []
        for deployment in inactive_deployments:
            try:
                inactive_items.append(
                    ListItemData(
                        id=deployment.deployment_id,
                        name=deployment.deployment_name or "Unknown",
                        status=None,
                        data=deployment,
                    )
                )
            except Exception:
                continue

        # Update list
        if inactive_items:
            self._list_view.update_items(inactive_items)
            # Try to find selected deployment
            if self.selected_deployment_id:
                try:
                    current_index = next(
                        (idx for idx, item in enumerate(inactive_items) if item.id == self.selected_deployment_id),
                        None,
                    )
                    if current_index is not None:
                        self.call_after_refresh(
                            lambda idx=current_index: setattr(self._list_view, "index", idx)
                        )
                except StopIteration:
                    pass
        else:
            self._list_view._empty_message = "No inactive deployments"
            self._list_view.show_empty_message()
        self._list_view.hide_loading()

    def _handle_selection(self, item_data: ListItemData) -> None:
        """Handle deployment selection (Enter key pressed)"""
        self.selected_deployment_id = item_data.id

    def _handle_highlight(self, item_data: ListItemData) -> None:
        """Handle deployment highlight (arrow navigation)"""
        self.selected_deployment_id = item_data.id

    def action_cursor_up(self) -> None:
        """Move cursor up"""
        if self._list_view:
            self._list_view.action_cursor_up()

    def action_cursor_down(self) -> None:
        """Move cursor down"""
        if self._list_view:
            self._list_view.action_cursor_down()
