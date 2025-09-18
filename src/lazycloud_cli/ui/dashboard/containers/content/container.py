from datetime import datetime
from enum import StrEnum

from textual.app import ComposeResult
from textual.containers import Container, VerticalScroll
from textual.widgets import Static

from lazycloud_cli.api import api
from lazycloud_cli.ui.dashboard.components import SectionContainer
from lazycloud_cli.ui.dashboard.containers.content.deployment_details import (
    DeploymentDetailsContainer,
)
from lazycloud_cli.ui.dashboard.containers.content.service_details import (
    RestartServiceModal,
    ServiceDetailsContainer,
)
from lazycloud_cli.ui.dashboard.theme import theme
from shared.models.statuses import ServiceStatus


class DisplayMode(StrEnum):
    """Display mode for the content container."""

    DEPLOYMENT = "deployment"
    SERVICE = "service"


class ContentContainer(Container):
    """Main content container for displaying deployment details."""

    BINDINGS = [
        ("r", "restart_service", "Restart Service"),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.current_deployment_id = None
        self.current_deployment_name = None
        self.current_service_name = None
        self.display_mode = DisplayMode.DEPLOYMENT
        self._border_subtitle_timer = None
        self.base_title = "Details [3]"
        self._deployment_view = None
        self._service_view = None

    def compose(self) -> ComposeResult:
        """Create the content area."""
        yield VerticalScroll(id="content-scroll")

    def on_mount(self) -> None:
        """Style the container when mounted."""
        self.base_title = "Details [3]"
        self._update_border_subtitle()
        self.styles.width = theme.right_width
        self.styles.height = "100%"
        self.styles.border = theme.get_border()
        self.styles.background = theme.background
        self.styles.padding = theme.padding

        # Make the container focusable so it can receive key events
        self.can_focus = True

        self._start_border_subtitle_timer()
        scroll = self.query_one("#content-scroll", VerticalScroll)
        initial_section = SectionContainer("Overview")
        scroll.mount(initial_section)
        initial_section.mount(Static("Select a deployment to view details"))

    async def on_unmount(self) -> None:
        """Clean up when container unmounts."""
        if self._border_subtitle_timer:
            self._border_subtitle_timer.stop()

        # Clean up service view if it exists
        if self._service_view:
            await self._service_view.cleanup()
            self._service_view = None

    def _start_border_subtitle_timer(self) -> None:
        if not self._border_subtitle_timer:
            self._update_border_subtitle()
            self._border_subtitle_timer = self.set_interval(
                1, self._update_border_subtitle
            )

    def _update_border_subtitle(self) -> None:
        current_time = datetime.now().strftime("%H:%M:%S")
        self.border_title = self.base_title

        # Add restart command next to border_subtitle if viewing a service
        if self.display_mode == DisplayMode.SERVICE:
            self.border_subtitle = f"r: restart - 🕐 {current_time}"
        else:
            self.border_subtitle = f"🕐 {current_time}"

        self.refresh()

    async def update_content(self, deployment_id: str, deployment_name: str) -> None:
        self.current_deployment_id = deployment_id
        self.current_deployment_name = deployment_name
        self.display_mode = DisplayMode.DEPLOYMENT
        self.base_title = f"Deployment Details [3] - {deployment_name}"
        self._update_border_subtitle()

        # Clean up previous service view if it exists
        if self._service_view:
            await self._service_view.cleanup()
            self._service_view = None

        # Clear the scroll container
        scroll = self.query_one("#content-scroll", VerticalScroll)
        scroll.remove_children()

        try:
            response = api.deployments.get_deployment_status(deployment_id)
            self._deployment_view = DeploymentDetailsContainer(scroll)
            self._deployment_view.render(response.status)

        except Exception as e:
            error_widget = Static(
                f"[red]Error loading deployment details: {str(e)}[/red]"
            )
            scroll.mount(error_widget)

    async def update_service_content(
        self,
        deployment_id: str,
        service: ServiceStatus,
    ) -> None:
        self.current_deployment_id = deployment_id
        self.current_service_name = service.name
        self.display_mode = DisplayMode.SERVICE
        self.base_title = f"Service Details [3] - {service.name}"
        self._update_border_subtitle()

        if self._service_view:
            await self._service_view.cleanup()

        scroll = self.query_one("#content-scroll", VerticalScroll)
        scroll.remove_children()

        self._service_view = ServiceDetailsContainer(scroll)
        await self._service_view.render(service, deployment_id, self.run_worker)

        # Focus this container to receive key events
        self.focus()

    async def clear_content(self) -> None:
        if self._service_view:
            await self._service_view.cleanup()
            self._service_view = None

        self.current_deployment_id = None
        self.current_deployment_name = None
        self.display_mode = DisplayMode.DEPLOYMENT
        self.base_title = "Details [3]"
        self._update_border_subtitle()

        scroll = self.query_one("#content-scroll", VerticalScroll)
        scroll.remove_children()

        initial_section = SectionContainer("Overview")
        scroll.mount(initial_section)
        initial_section.mount(Static("Select a deployment to view details"))

    def action_restart_service(self) -> None:
        """Handle the restart service action."""
        # Only work if we're viewing a service
        if self.display_mode != DisplayMode.SERVICE:
            return

        if not self.current_deployment_id or not self.current_service_name:
            return

        # Show confirmation modal
        modal = RestartServiceModal(service_name=self.current_service_name)
        modal.deployment_id = self.current_deployment_id
        self.app.push_screen(modal)
