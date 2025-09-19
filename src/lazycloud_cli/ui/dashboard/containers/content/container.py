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
        self.border_title = "[3] Details"
        self._deployment_view = None
        self._service_view = None
        self._additional_border_subtitle = None
        # NOTE: faster reactivity than has_focus
        self._show_border_subtitle = False

    def compose(self) -> ComposeResult:
        """Create the content area."""
        yield VerticalScroll(id="content-scroll")

    def on_mount(self) -> None:
        """Style the container when mounted."""
        self._update_border_subtitle()
        self.styles.width = theme.right_width
        self.styles.height = "100%"
        self.styles.border = theme.get_border()
        self.styles.background = theme.background
        self.styles.padding = theme.padding
        self.can_focus = True

        self._start_border_subtitle_timer()
        scroll = self.query_one("#content-scroll", VerticalScroll)
        initial_section = SectionContainer("Overview")
        scroll.mount(initial_section)
        initial_section.mount(Static("Select a deployment to view details"))

    def on_focus(self) -> None:
        """Handle focus event."""
        self.styles.border = theme.get_border(focused=True)
        self._show_border_subtitle = True
        if self.app:
            self.app.query(
                "#deployments-container"
            ).first().styles.border = theme.get_border()
            self.app.query(
                "#services-container"
            ).first().styles.border = theme.get_border()

        self._update_border_subtitle()

    def on_blur(self) -> None:
        """Handle blur event."""
        self.styles.border = theme.get_border()
        self._show_border_subtitle = False
        self._update_border_subtitle()

    async def on_unmount(self) -> None:
        """Clean up when container unmounts."""
        if self._border_subtitle_timer:
            self._border_subtitle_timer.stop()

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
        time_subtitle = f"🕐 {current_time}"

        if self._additional_border_subtitle and self._show_border_subtitle:
            self.border_subtitle = (
                f"{self._additional_border_subtitle} • {time_subtitle}"
            )
        else:
            self.border_subtitle = time_subtitle

        self.refresh()

    async def update_content(self, deployment_id: str, deployment_name: str) -> None:
        self.current_deployment_id = deployment_id
        self.current_deployment_name = deployment_name
        self.display_mode = DisplayMode.DEPLOYMENT
        self.border_title = f"[3] Deployment Details - {deployment_name}"
        self._additional_border_subtitle = None
        self._update_border_subtitle()

        if self._service_view:
            await self._service_view.cleanup()
            self._service_view = None

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
        self.border_title = f"[3] Service Details - {service.name}"
        self._additional_border_subtitle = "r: Restart"
        self._update_border_subtitle()

        if self._service_view:
            await self._service_view.cleanup()

        scroll = self.query_one("#content-scroll", VerticalScroll)
        scroll.remove_children()

        self._service_view = ServiceDetailsContainer(scroll)
        await self._service_view.render(service, deployment_id, self.run_worker)

    async def clear_content(self) -> None:
        if self._service_view:
            await self._service_view.cleanup()
            self._service_view = None

        self.current_deployment_id = None
        self.current_deployment_name = None
        self.display_mode = DisplayMode.DEPLOYMENT
        self.border_title = "[3] Details"
        self._additional_border_subtitle = None
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
