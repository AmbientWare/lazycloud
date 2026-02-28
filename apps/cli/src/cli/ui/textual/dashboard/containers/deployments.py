from models.statuses import DeploymentStatus
from responses.deployments import DeploymentResponse
from textual.app import ComposeResult
from textual.reactive import reactive

from cli.api import api
from cli.api.base import APIError
from cli.config import config
from cli.ui.textual.components import Container, ListItemData, ListView
from cli.ui.textual.components.listview import ListItem
from cli.ui.textual.components.modals import ErrorModal
from cli.ui.textual.messages import DeploymentSelected, DeploymentsLoaded
from cli.ui.textual.theme import Icons


class DeploymentsContainer(Container):
    """Container for displaying and selecting deployments."""

    # Reactive attributes
    selected_deployment: reactive[DeploymentResponse | None] = reactive(None)
    deployment_status: reactive[DeploymentStatus | None] = reactive(None)

    BINDINGS = [
        ("up,k", "cursor_up", "Move up"),
        ("down,j", "cursor_down", "Move down"),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._list_view = None
        self._status_cache: dict[str, DeploymentStatus] = {}
        self.border_title = f"{Icons.ROCKET} [1] Deployments"
        self.workspace_id = config.active_workspace_id

    def compose(self) -> ComposeResult:
        """Create the deployments widget."""
        self._list_view = ListView(
            on_select=lambda item_data: (
                self.call_later(self._handle_selection, item_data),
                None,
            )[-1],
            on_highlight=self._handle_highlight,
            empty_message="No deployments found",
            id="deployments-list",
        )
        yield self._list_view

    async def on_mount(self) -> None:
        """Style the container when mounted and load deployments."""
        self.can_focus = True

        # Load deployments after mounting
        await self.load_deployments()

    async def on_focus(self) -> None:
        """Handle focus event."""
        self.border_subtitle = "↑↓/jk Navigate"

        # Ensure an item is highlighted in the list
        if self._list_view:
            self._list_view.ensure_highlighted()
            # Only update if we're switching from service view or if deployment changed
            if self._list_view.index is not None and self._list_view.index < len(
                self._list_view.children
            ):
                item = self._list_view.children[self._list_view.index]
                if isinstance(item, ListItem):
                    # Only fetch and update if it's a different deployment
                    if self.selected_deployment != item.item_data.data:
                        self._schedule_set_selected_deployment(item.item_data)

    def on_blur(self) -> None:
        """Handle blur event."""
        self.border_subtitle = None

    def on_click(self) -> None:
        """Handle mouse clicks - switch to deployments view."""
        self.app.action_switch_to_deployments()

    async def watch_selected_deployment(self, old_value, new_value) -> None:
        """React when deployment is selected - post message for other components"""
        if new_value:
            status = self.deployment_status
            if status and status.deployment_id != new_value.id:
                status = None
            # Post message instead of directly updating other containers
            self.post_message(
                DeploymentSelected(
                    deployment=new_value,
                    status=status,
                )
            )

    async def watch_deployment_status(self, _old_value, new_value) -> None:
        """Emit an update when selected deployment status has been fetched/refreshed."""
        if (
            new_value
            and self.selected_deployment
            and new_value.deployment_id == self.selected_deployment.id
        ):
            self.post_message(
                DeploymentSelected(
                    deployment=self.selected_deployment,
                    status=new_value,
                )
            )

    async def load_deployments(self) -> None:
        """Fetch and display deployments from API."""
        if not self._list_view:
            return

        self._list_view.show_loading("Loading deployments...")

        try:
            response = api.deployments.list_deployments(workspace_id=self.workspace_id)

            items = []
            if response.deployments:
                for deployment in response.deployments:
                    items.append(
                        ListItemData(
                            id=deployment.id,
                            name=deployment.name,
                            data=deployment,
                        )
                    )

            self._list_view.update_items(items)

            # Post message indicating whether deployments were found
            self.post_message(DeploymentsLoaded(has_deployments=len(items) > 0))

        except APIError as e:
            self._list_view.update_items([])
            self._list_view.hide_loading()
            self.post_message(DeploymentsLoaded(has_deployments=False))

            # Show appropriate error message based on status code
            if e.status_code == 401:
                error_modal = ErrorModal(
                    title="Authentication Failed",
                    message="Your session has expired or is invalid.\n\nPlease run 'lazycloud login' again.",
                    icon=Icons.LOCK_KEY,
                )
                # Exit the app when auth error modal is dismissed
                self.app.push_screen(error_modal, callback=lambda _: self.app.exit(1))
                return
            elif e.status_code and 500 <= e.status_code < 600:
                error_modal = ErrorModal(
                    title="Server Error",
                    message=f"The server encountered an error:\n\n{str(e)}\n\nPlease try again later.",
                    icon=Icons.WARNING,
                )
            else:
                error_modal = ErrorModal(
                    title="Failed to Load Deployments",
                    message=f"Could not load deployments:\n\n{str(e)}",
                    icon=Icons.WARNING,
                )

            self.app.push_screen(error_modal)
            return

        except ConnectionError:
            self._list_view.update_items([])
            self._list_view.hide_loading()
            self.post_message(DeploymentsLoaded(has_deployments=False))

            error_modal = ErrorModal(
                title="Connection Error",
                message=f"Cannot connect to server at {config.api_base_url}\n\nPlease check your network connection.",
                icon=Icons.NETWORKS,
            )
            self.app.push_screen(error_modal)
            return

        except Exception as e:
            # Log unexpected errors but show empty state
            self.log.error(f"Unexpected error loading deployments: {e}")
            self._list_view.update_items([])
            self._list_view.show_empty_message()
            self.post_message(DeploymentsLoaded(has_deployments=False))

        finally:
            self._list_view.hide_loading()

    async def _handle_selection(self, item_data: ListItemData) -> None:
        """Handle deployment selection (Enter key pressed)."""
        self._schedule_set_selected_deployment(item_data)

    def _handle_highlight(self, item_data: ListItemData) -> None:
        """Handle deployment highlight with api request debouncing."""
        self._selection_timer = self.handle_debounce(
            self._selection_timer,
            lambda: (self.call_later(self._fetch_deployment_status, item_data), None)[
                -1
            ],
        )

    async def _fetch_deployment_status(self, item_data: ListItemData) -> None:
        """Fetch deployment status after debounce delay."""
        self._selection_timer = None

        # Only update content if the highlight actually changed to a different item
        if self.selected_deployment != item_data.data:
            self._schedule_set_selected_deployment(item_data)

    def _schedule_set_selected_deployment(self, item_data: ListItemData) -> None:
        """Schedule deployment selection without blocking navigation handlers."""
        self.run_worker(
            self._set_selected_deployment(item_data),
            exclusive=False,
        )

    async def _set_selected_deployment(self, item_data: ListItemData) -> None:
        """Set selected deployment and fetch status with stale-response guard."""
        deployment = item_data.data
        deployment_id = item_data.id
        self.selected_deployment = deployment
        status = await self._get_status(deployment_id)

        if (
            self.selected_deployment
            and self.selected_deployment.id == deployment_id
        ):
            self.deployment_status = status

    async def _get_status(self, deployment_id: str) -> DeploymentStatus | None:
        """Get deployment status from cache or API."""
        cached = self._status_cache.get(deployment_id)
        if cached is not None:
            return cached

        try:
            status = await api.deployments.get_deployment_status(deployment_id)
            self._status_cache[deployment_id] = status.status
            return status.status
        except Exception as e:
            self.log.error(f"Failed to get deployment status: {e}")
            return None

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
