from datetime import datetime
from enum import StrEnum

from textual.app import ComposeResult
from textual.containers import Container, VerticalScroll
from textual.widgets import Static

from lazycloud_cli.api import api
from lazycloud_cli.ui.dashboard.containers.common import SectionContainer
from lazycloud_cli.ui.dashboard.containers.content.deployment import DeploymentView
from lazycloud_cli.ui.dashboard.containers.content.service import ServiceView
from lazycloud_cli.ui.dashboard.theme import theme
from shared.models.statuses import ServiceStatus


class DisplayMode(StrEnum):
    """Display mode for the content container."""

    DEPLOYMENT = "deployment"
    SERVICE = "service"


class ContentContainer(Container):
    """Main content container for displaying deployment details."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.current_deployment_id = None
        self.current_deployment_name = None
        self.display_mode = DisplayMode.DEPLOYMENT
        self._clock_timer = None
        self.base_title = "Details [3]"
        self._deployment_view = None
        self._service_view = None

    def compose(self) -> ComposeResult:
        """Create the content area."""
        yield VerticalScroll(id="content-scroll")

    def on_mount(self) -> None:
        """Style the container when mounted."""
        self.base_title = "Details [3]"
        self._update_clock()
        self.styles.width = theme.right_width
        self.styles.height = "100%"
        self.styles.border = theme.get_border()
        self.styles.background = theme.background
        self.styles.padding = theme.padding

        self._start_clock()
        scroll = self.query_one("#content-scroll", VerticalScroll)
        initial_section = SectionContainer("Overview")
        scroll.mount(initial_section)
        initial_section.mount(Static("Select a deployment to view details"))

    async def on_unmount(self) -> None:
        """Clean up when container unmounts."""
        if self._clock_timer:
            self._clock_timer.stop()

        # Clean up service view if it exists
        if self._service_view:
            await self._service_view.cleanup()
            self._service_view = None

    def _start_clock(self) -> None:
        if not self._clock_timer:
            self._update_clock()
            self._clock_timer = self.set_interval(1, self._update_clock)

    def _update_clock(self) -> None:
        current_time = datetime.now().strftime("%H:%M:%S")
        self.border_title = self.base_title
        self.border_subtitle = f"🕐 {current_time}"
        self.refresh()

    async def update_content(self, deployment_id: str, deployment_name: str) -> None:
        self.current_deployment_id = deployment_id
        self.current_deployment_name = deployment_name
        self.display_mode = DisplayMode.DEPLOYMENT
        self.base_title = f"Details [3] - {deployment_name}"
        self._update_clock()

        # Clean up previous service view if any
        if self._service_view:
            await self._service_view.cleanup()
            self._service_view = None

        # Get the scroll container and clear it
        scroll = self.query_one("#content-scroll", VerticalScroll)
        scroll.remove_children()

        try:
            response = api.deployments.get_deployment_status(deployment_id)
            self._deployment_view = DeploymentView(scroll)
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
        self.display_mode = DisplayMode.SERVICE
        self.base_title = f"Service Details [3] - {service.name}"
        self._update_clock()

        if self._service_view:
            await self._service_view.cleanup()

        scroll = self.query_one("#content-scroll", VerticalScroll)
        scroll.remove_children()

        self._service_view = ServiceView(scroll)
        await self._service_view.render(service, deployment_id, self.run_worker)

    async def clear_content(self) -> None:
        if self._service_view:
            await self._service_view.cleanup()
            self._service_view = None

        self.current_deployment_id = None
        self.current_deployment_name = None
        self.display_mode = DisplayMode.DEPLOYMENT
        self.base_title = "Details [3]"
        self._update_clock()

        scroll = self.query_one("#content-scroll", VerticalScroll)
        scroll.remove_children()

        initial_section = SectionContainer("Overview")
        scroll.mount(initial_section)
        initial_section.mount(Static("Select a deployment to view details"))
