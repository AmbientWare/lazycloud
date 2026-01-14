from responses.usage import (
    WorkspaceUsageSummary,
)
from textual.app import ComposeResult
from textual.reactive import reactive

from cli.ui.colors import Colors
from cli.ui.textual.components import Container
from cli.ui.textual.components.listview import (
    ListItem,
    ListItemData,
    ListView,
)
from cli.ui.views.helpers.formatters import format_short_date


class ActiveDeploymentsContainer(Container):
    """Container for active deployments list"""

    selected_deployment_id: reactive[str | None] = reactive(None)
    usage_data: reactive[WorkspaceUsageSummary | None] = reactive(None)

    BINDINGS = [
        ("up,k", "cursor_up", "Move up"),
        ("down,j", "cursor_down", "Move down"),
    ]

    def __init__(self):
        super().__init__(id="active-deployments-container")
        self._list_view: ListView | None = None
        self.border_title = (
            f"[1] [bold {Colors.Hex.success}]Active[/bold {Colors.Hex.success}]"
        )

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

    def watch_usage_data(self, usage_data: WorkspaceUsageSummary | None) -> None:
        """Update deployments list when usage data is loaded"""
        if not self._list_view:
            return

        if not usage_data or not usage_data.deployments:
            self._list_view._empty_message = "No active deployments found"
            self._list_view.show_empty_message()
            self._list_view.hide_loading()
            return

        # Filter active deployments and sort alphabetically
        active_deployments = [d for d in usage_data.deployments if d.status == "Active"]
        active_deployments.sort(key=lambda d: (d.deployment_name or "").lower())

        # Convert to list items
        active_items = []
        for deployment in active_deployments:
            try:
                active_items.append(
                    ListItemData(
                        id=deployment.deployment_id,
                        name=deployment.deployment_name or "Unknown",
                        status=None,
                        extra_text=format_short_date(deployment.deployed_at),
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
                        (
                            idx
                            for idx, item in enumerate(active_items)
                            if item.id == self.selected_deployment_id
                        ),
                        None,
                    )
                    if current_index is not None:
                        self.call_after_refresh(
                            lambda idx=current_index: setattr(
                                self._list_view, "index", idx
                            )
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
    usage_data: reactive[WorkspaceUsageSummary | None] = reactive(None)
    show_zero_cost: reactive[bool] = reactive(False)

    BINDINGS = [
        ("up,k", "cursor_up", "Move up"),
        ("down,j", "cursor_down", "Move down"),
        ("z", "toggle_zero_cost", "Toggle $0"),
    ]

    def __init__(self):
        super().__init__(id="inactive-deployments-container")
        self._list_view: ListView | None = None
        self._all_inactive_items: list[ListItemData] = []
        self._hidden_count: int = 0
        self.border_title = (
            f"[2] [bold {Colors.Hex.warning}]Inactive[/bold {Colors.Hex.warning}]"
        )

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
        self._update_subtitle()
        if self._list_view:
            self._list_view.show_loading("Loading deployments...")

    def _update_subtitle(self) -> None:
        """Update subtitle with current toggle state"""
        if self._hidden_count > 0:
            toggle_hint = "z: Show $0" if not self.show_zero_cost else "z: Hide $0"
            self.border_subtitle = f"↑↓/jk Navigate • {toggle_hint}"
        else:
            self.border_subtitle = "↑↓/jk Navigate"

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

    def watch_usage_data(self, usage_data: WorkspaceUsageSummary | None) -> None:
        """Update deployments list when usage data is loaded"""
        if not self._list_view:
            return

        if not usage_data or not usage_data.deployments:
            self._list_view._empty_message = "No inactive deployments found"
            self._list_view.show_empty_message()
            self._list_view.hide_loading()
            self._all_inactive_items = []
            self._hidden_count = 0
            self._update_subtitle()
            return

        # Filter inactive deployments and sort by name, then by date
        inactive_deployments = [
            d for d in usage_data.deployments if d.status == "Inactive"
        ]
        inactive_deployments.sort(
            key=lambda d: ((d.deployment_name or "").lower(), d.deleted_at or "")
        )

        # Convert to list items
        all_items = []
        for deployment in inactive_deployments:
            try:
                all_items.append(
                    ListItemData(
                        id=deployment.deployment_id,
                        name=deployment.deployment_name or "Unknown",
                        status=None,
                        extra_text=format_short_date(deployment.deleted_at),
                        data=deployment,
                    )
                )
            except Exception:
                continue

        # Store all items and calculate hidden count
        self._all_inactive_items = all_items
        non_zero_items = [
            item for item in all_items
            if item.data and item.data.usage.costs and item.data.usage.costs.total_cost > 0
        ]
        self._hidden_count = len(all_items) - len(non_zero_items)
        self._update_subtitle()

        # Apply filter based on current toggle state
        self._apply_filter()

    def _apply_filter(self) -> None:
        """Apply the zero-cost filter to the list"""
        if not self._list_view:
            return

        if self.show_zero_cost:
            items_to_show = self._all_inactive_items
        else:
            items_to_show = [
                item for item in self._all_inactive_items
                if item.data and item.data.usage.costs and item.data.usage.costs.total_cost > 0
            ]

        # Update list
        if items_to_show:
            self._list_view.update_items(items_to_show)
            # Try to find selected deployment
            if self.selected_deployment_id:
                try:
                    current_index = next(
                        (
                            idx
                            for idx, item in enumerate(items_to_show)
                            if item.id == self.selected_deployment_id
                        ),
                        None,
                    )
                    if current_index is not None:
                        self.call_after_refresh(
                            lambda idx=current_index: setattr(
                                self._list_view, "index", idx
                            )
                        )
                except StopIteration:
                    pass
        else:
            self._list_view._empty_message = "No inactive deployments with costs"
            self._list_view.show_empty_message()
        self._list_view.hide_loading()

    def action_toggle_zero_cost(self) -> None:
        """Toggle showing zero-cost deployments"""
        if self._hidden_count > 0:
            self.show_zero_cost = not self.show_zero_cost
            self._update_subtitle()
            self._apply_filter()

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
