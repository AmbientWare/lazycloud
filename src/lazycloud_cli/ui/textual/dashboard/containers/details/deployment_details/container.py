from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import LoadingIndicator, Static
from textual.worker import Worker

from lazycloud_cli.api.status import StatusAPI
from lazycloud_cli.ui.textual.components import SectionContainer
from lazycloud_cli.ui.textual.dashboard.containers.details.deployment_details.networks_table import (
    NetworksTable,
)
from lazycloud_cli.ui.textual.dashboard.containers.details.deployment_details.services_table import (
    ServicesTable,
)
from lazycloud_cli.ui.textual.dashboard.containers.details.deployment_details.volumes_table import (
    VolumesTable,
)
from lazycloud_cli.ui.textual.dashboard.containers.details.utils import get_status_color
from lazycloud_cli.ui.textual.theme import Icons
from shared.models.statuses import DeploymentStatus


class DeploymentDetailsContainer(Widget):
    """Handles deployment-specific UI rendering with real-time updates."""

    # Reactive properties
    deployment_status: reactive[DeploymentStatus | None] = reactive(None)
    deployment_id: reactive[str | None] = reactive(None)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._status_api: StatusAPI | None = None
        self._stream_task: Worker | None = None
        self._scroll: VerticalScroll | None = None
        self._initial_render_done = False
        self._stream_error: str | None = None

        # Store widget references for updates
        self._overview_widget: Static | None = None
        self._services_table: ServicesTable | None = None
        self._volumes_table: VolumesTable | None = None
        self._networks_table: NetworksTable | None = None

    def compose(self) -> ComposeResult:
        """Create the initial UI structure."""
        self._scroll = VerticalScroll(id="deployment-details-scroll")
        yield self._scroll

    def on_mount(self) -> None:
        """Start SSE stream connection when mounted."""
        self.call_after_refresh(self._show_loading)
        if self.deployment_id:
            self._start_stream()

    def _show_loading(self) -> None:
        """Show loading indicator after widget is fully mounted."""
        # Skip loading if we already have initial status to render
        if self.deployment_status and self._scroll and self._scroll.is_mounted:
            self._initial_render_done = True
            self._render_sections(self.deployment_status)
        elif self._scroll and self._scroll.is_mounted:
            self._scroll.mount(LoadingIndicator())

    def _show_error(self, message: str) -> None:
        """Show error message when status cannot be loaded."""
        if not self._scroll or not self._scroll.is_mounted:
            return
        self._scroll.remove_children()
        error_section = SectionContainer(f"{Icons.OVERVIEW} Status Unavailable")
        self._scroll.mount(error_section)
        error_section.mount(Static(f"[yellow]{message}[/yellow]", markup=True))

    async def on_unmount(self) -> None:
        """Clean up when unmounting."""
        await self.cleanup()

    async def watch_deployment_status(self, old_value, new_value) -> None:
        """React to deployment status changes."""
        if not new_value or not self.is_mounted:
            return

        # Render on initial set or updates
        if not self._initial_render_done:
            self._initial_render_done = True
            self._render_sections(new_value)
        elif old_value is not None:
            self._render_sections(new_value)

    async def watch_deployment_id(self, old_value, new_value) -> None:
        """React to deployment ID changes."""
        if new_value and new_value != old_value:
            # Cancel existing SSE stream if any
            await self.cleanup()
            # Start new SSE stream connection
            self._start_stream()

    def _start_stream(self) -> None:
        """Start SSE stream connection for real-time updates."""
        if self.deployment_id:
            if self._stream_task and not self._stream_task.is_finished:
                self._stream_task.cancel()

            self._stream_task = self.run_worker(
                self._connect_deployment_stream(), exclusive=True
            )

    def _render_sections(self, deployment: DeploymentStatus) -> None:
        """Render all sections with the deployment data."""
        if not self._scroll:
            return
        self._scroll.remove_children()

        # Overview section
        overview_content = self._build_overview_content(deployment)
        overview_section = SectionContainer(f"{Icons.OVERVIEW} Overview")
        self._scroll.mount(overview_section)
        self._overview_widget = Static("\n".join(overview_content).strip(), markup=True)
        overview_section.mount(self._overview_widget)

        # Services section
        if deployment.services:
            services_section = SectionContainer(f"{Icons.WRENCH} Services")
            self._scroll.mount(services_section)
            self._services_table = ServicesTable()
            services_section.mount(self._services_table)
            self._services_table.update_services(deployment)

        # Volumes section
        if deployment.volumes:
            volumes_section = SectionContainer(f"{Icons.SAVE} Volumes")
            self._scroll.mount(volumes_section)
            self._volumes_table = VolumesTable()
            volumes_section.mount(self._volumes_table)
            self._volumes_table.update_volumes(deployment)

        # Networks section
        if deployment.networks:
            networks_section = SectionContainer(f"{Icons.NETWORKS} Networks")
            self._scroll.mount(networks_section)
            self._networks_table = NetworksTable()
            networks_section.mount(self._networks_table)
            self._networks_table.update_networks(deployment)

    def _build_overview_content(self, deployment: DeploymentStatus) -> list[str]:
        # Use colored status
        color = get_status_color(deployment.status)
        status_text = f"[{color}]{deployment.status.upper()}[/{color}]"

        content = [
            f"Deployment Name: {deployment.deployment_name}",
            f"Status:          {status_text}",
            f"Services:        {deployment.ready_services}/{deployment.total_services} ready",
            f"Replicas:        {deployment.ready_replicas}/{deployment.total_replicas} running",
        ]

        if deployment.deployed_at:
            content.append(
                f"Deployed At:     {deployment.deployed_at.strftime('%Y-%m-%d %H:%M:%S')}"
            )

        if deployment.last_checked:
            content.append(
                f"Last Checked:    {deployment.last_checked.strftime('%Y-%m-%d %H:%M:%S')}"
            )

        return content

    def update_deployment(self, deployment: DeploymentStatus) -> None:
        """Update all sections with new deployment data."""
        if self._overview_widget is None:
            self._render_sections(deployment)
            return

        overview_content = self._build_overview_content(deployment)
        self._overview_widget.update("\n".join(overview_content).strip())

        if self._services_table and deployment.services:
            self._services_table.update_services(deployment)

        if self._volumes_table and deployment.volumes:
            self._volumes_table.update_volumes(deployment)

        if self._networks_table and deployment.networks:
            self._networks_table.update_networks(deployment)

    async def cleanup(self) -> None:
        """Clean up SSE stream connections and tasks."""
        if self._stream_task and not self._stream_task.is_finished:
            self._stream_task.cancel()
            self._stream_task.wait()
        self._stream_task = None
        self._initial_render_done = False
        self._stream_error = None

        if self._status_api:
            await self._status_api.disconnect()
            self._status_api = None

    async def _connect_deployment_stream(self) -> None:
        """Connect to SSE stream for real-time deployment updates."""
        if not self.deployment_id:
            return

        try:
            self._status_api = StatusAPI()

            def on_update(status: DeploymentStatus | None) -> None:
                """Handle incoming deployment status updates."""
                if status:
                    self._stream_error = None
                    self.app.call_later(self.update_deployment, status)

            def on_error(e: Exception) -> None:
                """Handle SSE stream errors."""
                self._stream_error = str(e)

            await self._status_api.stream_deployment_status(
                deployment_id=self.deployment_id,
                on_update=on_update,
                on_error=on_error,
            )

        except Exception as e:
            self._stream_error = str(e)
            # Show error if we don't have any status to display
            if not self._initial_render_done and self.is_mounted:
                self.app.call_later(
                    self._show_error,
                    f"Could not connect to status stream: {e}",
                )
