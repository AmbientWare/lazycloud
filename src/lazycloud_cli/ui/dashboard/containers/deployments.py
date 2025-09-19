from textual.app import ComposeResult
from textual.containers import Container
from textual.reactive import reactive

from lazycloud_cli.api import api
from lazycloud_cli.ui.dashboard.components import ListItemData, ListView
from lazycloud_cli.ui.dashboard.messages import DeploymentSelected
from lazycloud_cli.ui.dashboard.theme import theme


class DeploymentsContainer(Container):
    """Container for displaying and selecting deployments."""

    BINDINGS = [
        ("up", "cursor_up", "Move up"),
        ("down", "cursor_down", "Move down"),
        ("enter", "select_item", "Select"),
    ]

    selected_deployment_id = reactive(None)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._list_view = None
        self.border_title = "[1] Deployments"

    def compose(self) -> ComposeResult:
        """Create the deployments widget."""
        self._list_view = ListView(
            on_select=self._handle_selection,
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
        # Reset other container borders
        if self.app:
            self.app.query(
                "#services-container"
            ).first().styles.border = theme.get_border()
            self.app.query("#main-container").first().styles.border = theme.get_border()

    def on_blur(self) -> None:
        """Handle blur event."""
        self.styles.border = theme.get_border()
        self.border_subtitle = None

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
        """Handle deployment selection."""
        self.selected_deployment_id = item_data.id
        self.post_message(DeploymentSelected(item_data.id, item_data.name))

    async def refresh_deployments(self) -> None:
        """Refresh the deployments list."""
        await self.load_deployments()

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
