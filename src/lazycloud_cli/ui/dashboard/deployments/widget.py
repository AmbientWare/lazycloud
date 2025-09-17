"""
Deployments widget for the dashboard.
"""

from textual.app import ComposeResult
from textual.containers import Container
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Label, ListItem, ListView

from lazycloud_cli.api import api
from lazycloud_cli.ui.dashboard.components.listview import LcListView
from lazycloud_cli.ui.dashboard.theme import theme


class DeploymentItem(ListItem):
    """A selectable deployment item."""

    def __init__(self, deployment_id: str, name: str, state: str):
        super().__init__()
        self.deployment_id = deployment_id
        self.deployment_name = name
        self.deployment_state = state
        self.styles.background = theme.colors.background

    def compose(self) -> ComposeResult:
        yield Label(self.deployment_name)


class DeploymentsWidget(Container):
    """Widget for displaying and selecting deployments."""

    selected_deployment_id = reactive(None)

    class DeploymentSelected(Message):
        """Message emitted when a deployment is selected."""

        def __init__(self, deployment_id: str, deployment_name: str):
            super().__init__()
            self.deployment_id = deployment_id
            self.deployment_name = deployment_name

    def compose(self) -> ComposeResult:
        """Create the widget layout."""
        list_view = LcListView(id="deployments-list")
        yield list_view

    async def on_mount(self) -> None:
        """Load deployments when widget mounts."""
        await self.load_deployments()

    async def load_deployments(self) -> None:
        """Fetch and display deployments from API."""
        list_view = self.query_one("#deployments-list", ListView)
        list_view.clear()

        # Show loading indicator
        list_view.loading = True

        try:
            # Fetch deployments from API
            response = api.deployments.list_deployments()

            if not response.deployments:
                list_view.append(ListItem(Label("No deployments found")))
            else:
                for deployment in response.deployments:
                    item = DeploymentItem(
                        deployment_id=deployment.id,
                        name=deployment.name,
                        state=deployment.state,
                    )
                    list_view.append(item)

        except Exception as e:
            list_view.append(ListItem(Label(f"Error: {str(e)}")))

        finally:
            list_view.loading = False

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Handle deployment selection."""
        if isinstance(event.item, DeploymentItem):
            self.selected_deployment_id = event.item.deployment_id
            # Emit message for parent to handle
            self.post_message(
                self.DeploymentSelected(
                    event.item.deployment_id, event.item.deployment_name
                )
            )

    async def refresh_deployments(self) -> None:
        """Refresh the deployments list."""
        await self.load_deployments()
