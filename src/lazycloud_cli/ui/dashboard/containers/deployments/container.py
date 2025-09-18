from textual.app import ComposeResult
from textual.containers import Container
from textual.message import Message
from textual.reactive import reactive

from lazycloud_cli.api import api
from lazycloud_cli.ui.dashboard.components import ListItemData, ListView
from lazycloud_cli.ui.dashboard.theme import theme


class DeploymentsContainer(Container):
    """Container for displaying and selecting deployments."""

    selected_deployment_id = reactive(None)

    class DeploymentSelected(Message):
        """Message emitted when a deployment is selected."""

        def __init__(self, deployment_id: str, deployment_name: str):
            super().__init__()
            self.deployment_id = deployment_id
            self.deployment_name = deployment_name

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._list_view = None

    def compose(self) -> ComposeResult:
        """Create the deployments widget."""
        self._list_view = ListView(
            on_select=self._handle_selection,
            empty_message="No deployments found",
            id="deployments-list",
        )
        yield self._list_view

    def on_mount(self) -> None:
        """Style the container when mounted."""
        self.border_title = "Deployments [1]"
        self.styles.height = theme.vertical_split
        self.styles.border = theme.get_border()
        self.styles.background = theme.background
        self.styles.padding = theme.padding

    async def on_show(self) -> None:
        """Load deployments when container is shown."""
        await self.load_deployments()

    async def load_deployments(self) -> None:
        """Fetch and display deployments from API."""
        if not self._list_view:
            return

        # Show loading indicator
        self._list_view.show_loading("Loading deployments...")

        try:
            # Fetch deployments from API
            response = api.deployments.list_deployments()

            # Convert to ListItemData
            items = []
            if response.deployments:
                for deployment in response.deployments:
                    items.append(
                        ListItemData(
                            id=deployment.id,
                            name=deployment.name,
                            status=deployment.state,
                            data=deployment,  # Store the full deployment object
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
        # Emit message for parent to handle
        self.post_message(self.DeploymentSelected(item_data.id, item_data.name))

    async def refresh_deployments(self) -> None:
        """Refresh the deployments list."""
        await self.load_deployments()
