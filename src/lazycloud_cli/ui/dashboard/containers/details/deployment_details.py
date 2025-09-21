from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.reactive import reactive
from textual.widgets import Static
from textual.worker import Worker

from lazycloud_cli.api.status import StatusAPI
from lazycloud_cli.ui.dashboard.components import Container
from lazycloud_cli.ui.dashboard.components.section import SectionContainer
from lazycloud_cli.ui.dashboard.containers.details.utils import get_status_color
from shared.models.statuses import DeploymentStatus


class DeploymentDetailsContainer(Container):
    """Handles deployment-specific UI rendering with real-time updates."""

    # Reactive properties
    deployment_status: reactive[DeploymentStatus | None] = reactive(None)
    deployment_id: reactive[str | None] = reactive(None)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._status_api: StatusAPI | None = None
        self._ws_task: Worker | None = None
        self._scroll: VerticalScroll | None = None

        # Store widget references for updates
        self._overview_widget: Static | None = None
        self._services_widget: Static | None = None
        self._volumes_widget: Static | None = None
        self._networks_widget: Static | None = None

    def compose(self) -> ComposeResult:
        """Create the initial UI structure."""
        self._scroll = VerticalScroll(id="deployment-details-scroll")
        yield self._scroll

    def on_mount(self) -> None:
        """Start WebSocket connection when mounted."""
        # Start WebSocket for updates
        if self.deployment_id:
            self._start_websocket()

        # Defer initial render until after the widget tree is complete
        if self.deployment_status:
            self.call_after_refresh(self._render_initial_content)

    def _render_initial_content(self) -> None:
        """Render initial content after widget tree is ready."""
        if self.deployment_status and self._scroll and self._scroll.is_mounted:
            self._render_sections(self.deployment_status)

    async def on_unmount(self) -> None:
        """Clean up when unmounting."""
        await self.cleanup()

    async def watch_deployment_status(self, old_value, new_value) -> None:
        """React to deployment status changes."""
        # Only render on updates, not initial set
        if new_value and self.is_mounted and old_value is not None:
            self._render_sections(new_value)

    async def watch_deployment_id(self, old_value, new_value) -> None:
        """React to deployment ID changes."""
        if new_value and new_value != old_value:
            # Cancel existing WebSocket if any
            await self.cleanup()
            # Start new WebSocket connection
            self._start_websocket()

    def _start_websocket(self) -> None:
        """Start WebSocket connection for real-time updates."""
        if self.deployment_id:
            self._ws_task = self.run_worker(self._connect_deployment_websocket())

    def _render_sections(self, deployment: DeploymentStatus) -> None:
        """Render all sections with the deployment data."""
        if not self._scroll:
            return
        self._scroll.remove_children()

        # Overview section
        overview_content = self._build_overview_content(deployment)
        overview_section = SectionContainer("📦 Overview")
        self._scroll.mount(overview_section)
        self._overview_widget = Static("\n".join(overview_content).strip(), markup=True)
        overview_section.mount(self._overview_widget)

        # Services section
        if deployment.services:
            services_content = self._build_services_content(deployment)
            services_section = SectionContainer("🔧 Services")
            self._scroll.mount(services_section)
            self._services_widget = Static(
                "\n".join(services_content).strip(), markup=True
            )
            services_section.mount(self._services_widget)

        # Volumes section
        if deployment.volumes:
            volumes_content = self._build_volumes_content(deployment)
            volumes_section = SectionContainer("💾 Volumes")
            self._scroll.mount(volumes_section)
            self._volumes_widget = Static(
                "\n".join(volumes_content).strip(), markup=True
            )
            volumes_section.mount(self._volumes_widget)

        # Networks section
        if deployment.networks:
            networks_content = self._build_networks_content(deployment)
            networks_section = SectionContainer("🌐 Networks")
            self._scroll.mount(networks_section)
            self._networks_widget = Static(
                "\n".join(networks_content).strip(), markup=True
            )
            networks_section.mount(self._networks_widget)

    def _build_overview_content(self, deployment: DeploymentStatus) -> list[str]:
        status_color = get_status_color(deployment.status)

        content = [
            f"Deployment Name: {deployment.deployment_name}",
            f"Status:          [{status_color}]{deployment.status.upper()}[/{status_color}]",
            f"Ready:           {'✅ Yes' if deployment.ready else '⏳ No'}",
            f"Services:        {deployment.ready_services}/{deployment.total_services} ready",
            f"Replicas:        {deployment.ready_replicas}/{deployment.total_replicas} running",
        ]

        if deployment.last_checked:
            content.append(
                f"Last Checked:    {deployment.last_checked.strftime('%Y-%m-%d %H:%M:%S')}"
            )

        return content

    def _build_services_content(self, deployment: DeploymentStatus) -> list[str]:
        content = []
        for service in deployment.services:
            status_color = get_status_color(service.status)

            service_line = (
                f"● {service.name}: [{status_color}]{service.status.upper()}[/{status_color}] "
                f"({service.ready_replicas}/{service.total_replicas} replicas)"
            )

            if service.restarts > 0:
                service_line += f" - {service.restarts} restarts"

            content.append(service_line)
        return content

    def _build_volumes_content(self, deployment: DeploymentStatus) -> list[str]:
        return [f"● {v.name} - {v.status}" for v in deployment.volumes]

    def _build_networks_content(self, deployment: DeploymentStatus) -> list[str]:
        return [f"● {n.name} - {n.status}" for n in deployment.networks]

    def _create_section(self, title: str, content: list[str]) -> None:
        if not self._scroll:
            return
        section = SectionContainer(title)
        self._scroll.mount(section)

        text_content = "\n".join(content).strip()
        widget = Static(text_content, markup=True)
        section.mount(widget)

    def update_deployment(self, deployment: DeploymentStatus) -> None:
        """Update all sections with new deployment data."""
        self.deployment_status = deployment

        # Update overview
        if self._overview_widget:
            overview_content = self._build_overview_content(deployment)
            self._overview_widget.update("\n".join(overview_content).strip())

        # Update services
        if self._services_widget and deployment.services:
            services_content = self._build_services_content(deployment)
            self._services_widget.update("\n".join(services_content).strip())

        # Update volumes
        if self._volumes_widget and deployment.volumes:
            volumes_content = self._build_volumes_content(deployment)
            self._volumes_widget.update("\n".join(volumes_content).strip())

        # Update networks
        if self._networks_widget and deployment.networks:
            networks_content = self._build_networks_content(deployment)
            self._networks_widget.update("\n".join(networks_content).strip())

    async def cleanup(self) -> None:
        """Clean up WebSocket connections and tasks."""
        if self._ws_task:
            self._ws_task.cancel()
            self._ws_task = None

        if self._status_api:
            await self._status_api.disconnect()
            self._status_api = None

    async def _connect_deployment_websocket(self) -> None:
        """Connect to WebSocket for real-time deployment updates."""
        if not self.deployment_id:
            return

        try:
            self._status_api = StatusAPI()

            def on_update(status: DeploymentStatus | None) -> None:
                """Handle incoming deployment status updates."""
                if status:
                    # Use call_later to ensure UI updates happen on the main thread
                    self.app.call_later(self.update_deployment, status)

            def on_error(_: Exception) -> None:
                """Handle WebSocket errors."""
                pass

            await self._status_api.stream_deployment_status(
                deployment_id=self.deployment_id,
                on_update=on_update,
                on_error=on_error,
            )

        except Exception:
            pass
