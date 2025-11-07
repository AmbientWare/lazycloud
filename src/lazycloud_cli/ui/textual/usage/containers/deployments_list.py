from textual.app import ComposeResult
from textual.reactive import reactive

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


class DeploymentsListView(Container):
    """List view showing workspace deployments"""

    selected_deployment_id: reactive[str | None] = reactive(None)
    usage_data: reactive[WorkspaceUsageWithDeploymentsResponse | None] = reactive(None)

    BINDINGS = [
        ("up,k", "cursor_up", "Move up"),
        ("down,j", "cursor_down", "Move down"),
    ]

    def __init__(self):
        super().__init__(id="deployments-list-container")
        self._list_view: ListView | None = None
        self.border_title = f"{Icons.ROCKET} [1] Deployments"

    def compose(self) -> ComposeResult:
        """Compose the list view"""
        self._list_view = ListView(
            id="deployments-list",
            on_select=self._handle_selection,
            on_highlight=self._handle_highlight,
        )
        yield self._list_view

    def on_mount(self) -> None:
        """Set up when mounted"""
        self.can_focus = True
        if self._list_view:
            self._list_view.show_loading("Loading deployments...")

    def on_focus(self) -> None:
        """Handle focus event"""
        self.border_subtitle = "↑↓/jk Navigate"
        if self._list_view:
            self._list_view.ensure_highlighted()
            # Update selected_deployment_id if list view has a highlighted item
            if self._list_view.index is not None and self._list_view.index < len(
                self._list_view.children
            ):
                item = self._list_view.children[self._list_view.index]
                if isinstance(item, ListItem) and item.item_data:
                    # Only update if it's different to avoid unnecessary updates
                    if self.selected_deployment_id != item.item_data.id:
                        self.selected_deployment_id = item.item_data.id

    def on_blur(self) -> None:
        """Handle blur event"""
        self.border_subtitle = ""

    def watch_usage_data(
        self, usage_data: WorkspaceUsageWithDeploymentsResponse | None
    ) -> None:
        """Update deployments list when usage data is loaded"""
        if not self._list_view:
            return

        if not usage_data or not usage_data.deployments:
            self._list_view._empty_message = "No deployments with usage found"
            self._list_view.show_empty_message()
            self._list_view.hide_loading()
            return

        # Convert deployment usage overviews to list items
        items = []
        for deployment in usage_data.deployments:
            try:
                items.append(
                    ListItemData(
                        id=deployment.deployment_id,
                        name=deployment.deployment_name or "Unknown",
                        status=None,
                        data=deployment,
                    )
                )
            except Exception:
                continue

        if not items:
            self._list_view._empty_message = "No valid deployments found"
            self._list_view.show_empty_message()
            self._list_view.hide_loading()
            return

        self._list_view.update_items(items)
        self._list_view.hide_loading()

        # Auto-select and highlight first deployment if none selected
        # Use call_after_refresh to ensure ListView has rendered before setting index
        if items and self._list_view:
            if not self.selected_deployment_id:
                # Set selected_deployment_id first, then highlight after refresh
                self.selected_deployment_id = items[0].id
                self.call_after_refresh(lambda: setattr(self._list_view, "index", 0))
            else:
                # If already selected, ensure it's highlighted
                try:
                    current_index = next(
                        (
                            idx
                            for idx, item in enumerate(items)
                            if item.id == self.selected_deployment_id
                        ),
                        0,
                    )
                    self.call_after_refresh(
                        lambda: setattr(self._list_view, "index", current_index)
                    )
                except StopIteration:
                    # Selected deployment not in list, select first
                    self.selected_deployment_id = items[0].id
                    self.call_after_refresh(
                        lambda: setattr(self._list_view, "index", 0)
                    )

    def _handle_selection(self, item_data: ListItemData) -> None:
        """Handle deployment selection (Enter key pressed)"""
        self.selected_deployment_id = item_data.id

    def _handle_highlight(self, item_data: ListItemData) -> None:
        """Handle deployment highlight (arrow navigation)"""
        self.selected_deployment_id = item_data.id

    def action_cursor_up(self) -> None:
        """Move cursor up in the list"""
        if self._list_view:
            self._list_view.action_cursor_up()

    def action_cursor_down(self) -> None:
        """Move cursor down in the list"""
        if self._list_view:
            self._list_view.action_cursor_down()
