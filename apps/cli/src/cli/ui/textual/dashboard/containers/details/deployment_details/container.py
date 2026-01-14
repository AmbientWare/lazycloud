import asyncio

from loguru import logger
from models.statuses import DeploymentStatus
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Static
from textual.worker import Worker, WorkerCancelled

from cli.api import api
from cli.ui.textual.components import SectionContainer
from cli.ui.textual.dashboard.containers.details.deployment_details.networks_table import (
    NetworksTable,
)
from cli.ui.textual.dashboard.containers.details.deployment_details.services_table import (
    ServicesTable,
)
from cli.ui.textual.dashboard.containers.details.deployment_details.volumes_table import (
    VolumesTable,
)
from cli.ui.textual.dashboard.containers.details.utils import get_status_color
from cli.ui.textual.messages import DeploymentStatusUpdated
from cli.ui.textual.theme import Icons


class DeploymentDetailsContainer(Widget):
    """Handles deployment-specific UI rendering with real-time updates."""

    def __init__(
        self,
        deployment_id: str,
        deployment_status: DeploymentStatus,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.deployment_id = deployment_id
        self.deployment_status = deployment_status
        self._stream_task: Worker | None = None
        self._scroll: VerticalScroll | None = None

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
        """Render content and start streaming when mounted."""
        self._render_sections(self.deployment_status)
        self._start_stream()

    async def on_unmount(self) -> None:
        """Clean up when unmounting."""
        await self._stop_stream()

    def _start_stream(self) -> None:
        """Start SSE stream for real-time updates."""
        self._stream_task = self.run_worker(
            self._connect_deployment_stream(),
            exclusive=True,
        )

    async def _stop_stream(self) -> None:
        """Stop the SSE stream."""
        if self._stream_task and not self._stream_task.is_finished:
            self._stream_task.cancel()
            try:
                await self._stream_task.wait()
            except (WorkerCancelled, Exception):
                pass
        self._stream_task = None

    def _render_sections(self, deployment: DeploymentStatus) -> None:
        """Render all sections with the deployment data."""
        if not self._scroll:
            return
        self._scroll.remove_children()

        # Overview section
        overview_content = self._build_overview_content(deployment)
        self._overview_widget = Static("\n".join(overview_content).strip(), markup=True)
        overview_section = SectionContainer(
            f"{Icons.OVERVIEW} Overview", self._overview_widget
        )
        self._scroll.mount(overview_section)

        # Services section
        if deployment.services:
            self._services_table = ServicesTable()
            services_section = SectionContainer(
                f"{Icons.WRENCH} Services", self._services_table
            )
            self._scroll.mount(services_section)
            self._services_table.update_services(deployment)

        # Volumes section
        if deployment.volumes:
            self._volumes_table = VolumesTable()
            volumes_section = SectionContainer(
                f"{Icons.SAVE} Volumes", self._volumes_table
            )
            self._scroll.mount(volumes_section)
            self._volumes_table.update_volumes(deployment)

        # Networks section
        if deployment.networks:
            self._networks_table = NetworksTable()
            networks_section = SectionContainer(
                f"{Icons.NETWORKS} Networks", self._networks_table
            )
            self._scroll.mount(networks_section)
            self._networks_table.update_networks(deployment)

    def _build_overview_content(self, deployment: DeploymentStatus) -> list[str]:
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

    def _update_from_stream(self, deployment: DeploymentStatus) -> None:
        """Update UI from stream data."""
        if not self.is_mounted:
            return

        # Update cached state in parent containers
        self.post_message(
            DeploymentStatusUpdated(
                deployment_id=self.deployment_id,
                status=deployment,
            )
        )

        if self._overview_widget:
            overview_content = self._build_overview_content(deployment)
            self._overview_widget.update("\n".join(overview_content).strip())

        if self._services_table and deployment.services:
            self._services_table.update_services(deployment)

        if self._volumes_table and deployment.volumes:
            self._volumes_table.update_volumes(deployment)

        if self._networks_table and deployment.networks:
            self._networks_table.update_networks(deployment)

    async def _connect_deployment_stream(self) -> None:
        """Connect to SSE stream for real-time deployment updates."""
        max_reconnect_attempts = 5
        reconnect_delay = 3

        for attempt in range(max_reconnect_attempts):
            try:
                await api.status.stream_deployment_status(
                    deployment_id=self.deployment_id,
                    on_update=lambda data: self.app.call_later(
                        self._update_from_stream, data
                    ),
                    on_error=lambda e: logger.warning(
                        f"SSE stream error for deployment {self.deployment_id}: {e}"
                    ),
                )
                break

            except Exception as e:
                logger.error(
                    f"SSE connection failed for deployment {self.deployment_id} "
                    f"(attempt {attempt + 1}/{max_reconnect_attempts}): {e}"
                )

                if attempt < max_reconnect_attempts - 1 and self.is_mounted:
                    await asyncio.sleep(reconnect_delay)
                else:
                    break
