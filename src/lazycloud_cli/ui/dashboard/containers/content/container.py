from datetime import datetime
from enum import StrEnum

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.reactive import reactive
from textual.widgets import Static

from lazycloud_cli.ui.dashboard.components import Container, SectionContainer
from lazycloud_cli.ui.dashboard.containers.content.deployment_details import (
    DeploymentDetailsContainer,
)
from lazycloud_cli.ui.dashboard.containers.content.service_details import (
    RestartServiceModal,
    ServiceDetailsContainer,
)
from lazycloud_cli.ui.dashboard.theme import theme
from shared.models.statuses import DeploymentStatus, ServiceStatus
from shared.responses.deployments import DeploymentResponse


class DisplayMode(StrEnum):
    DEPLOYMENT = "deployment"
    SERVICE = "service"


class ContentContainer(Container):
    """Main content container for displaying deployment details."""

    # Reactive attributes
    deployment: reactive[DeploymentResponse | None] = reactive(None)
    deployment_status: reactive[DeploymentStatus | None] = reactive(None)
    service: reactive[ServiceStatus | None] = reactive(None)
    display_mode: reactive[str] = reactive(DisplayMode.DEPLOYMENT)

    BINDINGS = [
        ("r", "restart_service", "Restart Service"),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._border_subtitle_timer = None
        self.border_title = "[3] Details"
        self._deployment_view = None
        self._service_view = None
        # NOTE: faster reactivity than has_focus
        self._show_border_subtitle = False
        self._service_subtitle = "r: Restart"
        self._logs_subtitle = "4: Instances"

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
        scroll = self.query_one(VerticalScroll)
        initial_section = SectionContainer("Overview")
        scroll.mount(initial_section)
        initial_section.mount(Static("Select a deployment to view details"))

    def on_focus(self) -> None:
        """Handle focus event."""
        self.styles.border = theme.get_border(focused=True)
        self._show_border_subtitle = True

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
        navigation_subtitle = "1: Deployments • 2: Services • 3: Details"

        parts = [navigation_subtitle]

        if self.display_mode == DisplayMode.SERVICE:
            if self._show_border_subtitle and self._service_subtitle:
                parts.insert(0, self._service_subtitle)
            parts.append(self._logs_subtitle)

        parts.append(time_subtitle)
        self.border_subtitle = " • ".join(parts)
        self.refresh()

    async def watch_deployment(self, _old_value, new_value) -> None:
        """Auto-refresh when deployment changes"""
        if new_value and self.display_mode == DisplayMode.DEPLOYMENT:
            await self.refresh_deployment_content()

    async def watch_service(self, _old_value, new_value) -> None:
        """Auto-refresh when service changes"""
        if new_value and self.display_mode == DisplayMode.SERVICE:
            await self.refresh_service_content()

    async def watch_display_mode(self, _old_value, new_value) -> None:
        """Switch content based on display mode"""
        if new_value == DisplayMode.DEPLOYMENT and self.deployment:
            await self.refresh_deployment_content()
        elif new_value == DisplayMode.SERVICE and self.service:
            await self.refresh_service_content()

    async def refresh_deployment_content(self) -> None:
        if not self.deployment:
            return

        self.border_title = f"[3] Deployment Details - {self.deployment.name}"
        self._update_border_subtitle()

        if self._service_view:
            await self._service_view.cleanup()
            self._service_view = None

        scroll = self.query_one(VerticalScroll)
        scroll.remove_children()

        # only fetch the deployment status if the deployment is not already loaded
        try:
            self._deployment_view = DeploymentDetailsContainer(scroll)
            self._deployment_view.render(self.deployment_status)

        except Exception as e:
            error_widget = Static(
                f"[red]Error loading deployment details: {str(e)}[/red]"
            )
            scroll.mount(error_widget)

    async def refresh_service_content(self) -> None:
        if not self.service or not self.deployment:
            return

        self.border_title = f"[3] Service Details - {self.service.name}"
        self._update_border_subtitle()

        if self._service_view:
            await self._service_view.cleanup()

        scroll = self.query_one(VerticalScroll)
        scroll.remove_children()

        self._service_view = ServiceDetailsContainer(scroll)
        await self._service_view.render(
            self.service,
            self.deployment.id,
            self.run_worker,
        )

    async def clear_content(self) -> None:
        if self._service_view:
            await self._service_view.cleanup()
            self._service_view = None

        self.border_title = "[3] Details"
        self._update_border_subtitle()

        scroll = self.query_one(VerticalScroll)
        scroll.remove_children()

        initial_section = SectionContainer("Overview")
        scroll.mount(initial_section)
        initial_section.mount(Static("Select a deployment to view details"))

    def action_restart_service(self) -> None:
        """Handle the restart service action."""
        if not self.service or not self.deployment:
            return

        # Show confirmation modal
        modal = RestartServiceModal(
            service_name=self.service.name,
            deployment_id=self.deployment.id,
        )
        self.app.push_screen(modal)
