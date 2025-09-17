"""
Content container for the dashboard.
"""

from datetime import datetime

from textual.app import ComposeResult
from textual.containers import Container, VerticalScroll
from textual.widgets import Static

from lazycloud_cli.api import api
from lazycloud_cli.ui.dashboard.containers.common import SectionContainer
from lazycloud_cli.ui.dashboard.containers.content.deployment import DeploymentView
from lazycloud_cli.ui.dashboard.containers.content.service import ServiceView
from lazycloud_cli.ui.dashboard.theme import theme
from shared.models.statuses import ServiceStatus


class ContentContainer(Container):
    """Main content container for displaying deployment details."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.current_deployment_id = None
        self.current_deployment_name = None
        self.display_mode = "deployment"  # "deployment" or "service"
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
        self._update_title_with_clock()
        self.styles.width = theme.right_width
        self.styles.height = "100%"
        self.styles.border = theme.get_border()
        self.styles.background = theme.background
        self.styles.padding = theme.padding

        # Start the clock
        self._start_clock()

        # Show initial message
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
        """Start the clock timer."""
        if not self._clock_timer:
            self._update_title_with_clock()
            self._clock_timer = self.set_interval(1, self._update_title_with_clock)

    def _update_title_with_clock(self) -> None:
        """Update the title with current time."""
        current_time = datetime.now().strftime("%H:%M:%S")
        # Keep the title as is, put clock in the bottom border
        self.border_title = self.base_title
        self.border_subtitle = f"🕐 {current_time}"
        self.refresh()

    async def update_content(self, deployment_id: str, deployment_name: str) -> None:
        """Update the content when a deployment is selected."""
        self.current_deployment_id = deployment_id
        self.current_deployment_name = deployment_name
        self.display_mode = "deployment"
        self.base_title = f"Details [3] - {deployment_name}"
        self._update_title_with_clock()

        # Clean up previous service view if any
        if self._service_view:
            await self._service_view.cleanup()
            self._service_view = None

        # Get the scroll container and clear it
        scroll = self.query_one("#content-scroll", VerticalScroll)
        scroll.remove_children()

        try:
            # Fetch deployment status
            response = api.deployments.get_deployment_status(deployment_id)

            # Use DeploymentView to render
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
        """Update the content to show service details."""
        self.display_mode = "service"
        self.base_title = f"Service Details [3] - {service.name}"
        self._update_title_with_clock()

        # Clean up previous service view if any
        if self._service_view:
            await self._service_view.cleanup()

        # Get the scroll container and clear it
        scroll = self.query_one("#content-scroll", VerticalScroll)
        scroll.remove_children()

        # Use ServiceView to render
        self._service_view = ServiceView(scroll)
        await self._service_view.render(service, deployment_id, self.run_worker)

    async def clear_content(self) -> None:
        """Clear the content area."""
        # Clean up service view if it exists
        if self._service_view:
            await self._service_view.cleanup()
            self._service_view = None

        self.current_deployment_id = None
        self.current_deployment_name = None
        self.display_mode = "deployment"
        self.base_title = "Details [3]"
        self._update_title_with_clock()

        scroll = self.query_one("#content-scroll", VerticalScroll)
        scroll.remove_children()

        # Reuse the same pattern as on_mount
        initial_section = SectionContainer("Overview")
        scroll.mount(initial_section)
        initial_section.mount(Static("Select a deployment to view details"))
