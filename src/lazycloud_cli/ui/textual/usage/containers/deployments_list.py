import asyncio

from textual.app import ComposeResult
from textual.reactive import reactive

from lazycloud_cli.api import api
from lazycloud_cli.config import config
from lazycloud_cli.ui.textual.components import Container
from lazycloud_cli.ui.textual.components.listview import ListItemData, ListView
from lazycloud_cli.ui.textual.theme import Icons


class DeploymentsListView(Container):
    """List view showing workspace deployments"""

    selected_deployment_id: reactive[str | None] = reactive(None)

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
        self._list_view = ListView(id="deployments-list")
        yield self._list_view

    def on_mount(self) -> None:
        """Fetch deployments when mounted"""
        self.can_focus = True
        self.run_worker(self._fetch_deployments_async(), exclusive=True)

    def on_focus(self) -> None:
        """Handle focus event"""
        self.border_subtitle = "↑↓/jk Navigate"
        if self._list_view:
            self._list_view.ensure_highlighted()

    def on_blur(self) -> None:
        """Handle blur event"""
        self.border_subtitle = ""

    async def _fetch_deployments_async(self) -> None:
        """Fetch deployments from API"""
        try:
            if not self._list_view:
                return

            self._list_view.show_loading("Loading deployments...")

            # Fetch deployments using existing API
            response = await asyncio.to_thread(
                api.deployments.list_deployments, config.active_workspace_id
            )

            if not response.deployments:
                self._list_view._empty_message = "No deployments found"
                self._list_view.show_empty_message()
                return

            # Convert to list items
            items = []
            for dep in response.deployments:
                try:
                    # Extract status string safely
                    status_str = None
                    if hasattr(dep, "status") and dep.status:
                        if hasattr(dep.status, "status"):
                            status_str = dep.status.status
                        elif isinstance(dep.status, str):
                            status_str = dep.status

                    items.append(
                        ListItemData(
                            id=dep.id,
                            name=dep.name or "Unknown",
                            status=status_str,
                            data=dep,
                        )
                    )
                except Exception:
                    # Skip deployments that fail to parse
                    continue

            if not items:
                self._list_view._empty_message = "No valid deployments found"
                self._list_view.show_empty_message()
                return

            self._list_view.update_items(items)

            # Auto-select and highlight first deployment
            if items and self._list_view:
                self._list_view.index = 0
                self.selected_deployment_id = items[0].id

        except Exception as e:
            if self._list_view:
                self._list_view._empty_message = f"{str(e)}"
                self._list_view.show_empty_message()

        finally:
            if self._list_view:
                self._list_view.hide_loading()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Handle deployment selection"""
        self.selected_deployment_id = event.item.id

    def action_cursor_up(self) -> None:
        """Move cursor up in the list"""
        if self._list_view:
            self._list_view.action_cursor_up()

    def action_cursor_down(self) -> None:
        """Move cursor down in the list"""
        if self._list_view:
            self._list_view.action_cursor_down()
