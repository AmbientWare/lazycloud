import asyncio

from loguru import logger
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.reactive import reactive
from textual.widgets import Static
from textual.worker import Worker

from lazycloud_cli.api import api
from lazycloud_cli.ui.dashboard.components import Container
from lazycloud_cli.ui.dashboard.components.section import SectionContainer
from lazycloud_cli.ui.dashboard.containers.details.service_details.pods_table import (
    PodTable,
)
from lazycloud_cli.ui.dashboard.containers.details.utils import get_status_color
from lazycloud_cli.utils.utils import format_image_name
from shared.models.helm import HealthCheckValues, HPAValues
from shared.models.k8s import Resources
from shared.models.statuses import PodStatus, ServiceStatus


class ServiceDetailsContainer(Container):
    """Handles service-specific UI rendering and updates."""

    # Reactive properties
    service_status: reactive[ServiceStatus | None] = reactive(None)
    deployment_id: reactive[str | None] = reactive(None)
    service_name: reactive[str | None] = reactive(None)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._overview_widget: Static | None = None
        self._pods_table: PodTable | None = None
        self._stream_task: Worker | None = None
        self._scroll: VerticalScroll | None = None

    def compose(self) -> ComposeResult:
        """Create the initial UI structure."""
        self._scroll = VerticalScroll(id="service-details-scroll")
        yield self._scroll

    def on_mount(self) -> None:
        """Start SSE stream connection when mounted."""
        # Start the SSE stream for real-time updates
        if self.deployment_id and self.service_name:
            self._start_stream()

        # Defer initial render until after the widget tree is complete
        if self.service_status:
            self.call_after_refresh(self._render_initial_content)

    def _render_initial_content(self) -> None:
        """Render initial content after widget tree is ready."""
        if self.service_status and self._scroll and self._scroll.is_mounted:
            self._render_sections(self.service_status)

    async def on_unmount(self) -> None:
        """Clean up when unmounting."""
        await self.cleanup()

    async def watch_service_status(self, old_value, new_value) -> None:
        """React to service status changes."""
        # Only render on updates, not initial set
        if new_value and self.is_mounted and old_value is not None:
            self._render_sections(new_value)

    async def watch_service_name(self, old_value, new_value) -> None:
        """React to service name changes."""
        if new_value and new_value != old_value:
            # Clean up old connection first
            await self.cleanup()
            self._start_stream()

    def _start_stream(self) -> None:
        """Start SSE stream connection for real-time updates."""
        if self.deployment_id and self.service_name:
            # Cancel existing stream if any
            if self._stream_task and not self._stream_task.is_finished:
                self._stream_task.cancel()

            # Start new stream worker
            self._stream_task = self.run_worker(
                self._connect_service_stream(), exclusive=True
            )

    def _render_sections(self, service: ServiceStatus) -> None:
        """Render all sections with the service data."""
        if not self._scroll:
            return
        self._scroll.remove_children()

        overview_content = self._build_overview_content(service)
        overview_section = SectionContainer("📦 Overview")
        self._scroll.mount(overview_section)
        self._overview_widget = Static("\n".join(overview_content).strip(), markup=True)
        overview_section.mount(self._overview_widget)

        if service.ports:
            ports_content = self._build_ports_content(service.ports)
            self._create_section("🌐 Network Ports", ports_content)

        if service.resources and (
            service.resources.limits or service.resources.requests
        ):
            resources_content = self._build_resources_content(service.resources)
            self._create_section("💻 Resource Configuration", resources_content)

        if service.healthcheck:
            health_content = self._build_health_content(service.healthcheck)
            self._create_section("❤️ Health Checks", health_content)

        autoscaling_content = self._build_autoscaling_content(service.hpa)
        self._create_section("🔄 Auto-scaling", autoscaling_content)

        self._pods_table = self._create_pods_table(self.deployment_id, service.name)
        if service.pods:
            self._pods_table.update_pods(service.pods)

    def update_overview(self, service: ServiceStatus) -> None:
        """Update the overview widget with new service data."""
        if self._overview_widget:
            overview_content = self._build_overview_content(service)
            self._overview_widget.update("\n".join(overview_content).strip())

    def update_pods_table(self, pods: list[PodStatus]) -> None:
        """Update the pods table with new data."""
        if self._pods_table:
            self._pods_table.update_pods(pods)

    def action_focus_instances(self) -> None:
        """Focus the instances table."""
        if self._pods_table:
            self._pods_table.focus()

    async def cleanup(self) -> None:
        """Clean up SSE stream connections and tasks."""
        if self._stream_task and not self._stream_task.is_finished:
            self._stream_task.cancel()
            self._stream_task.wait()
        self._stream_task = None

    def _build_overview_content(self, service: ServiceStatus) -> list[str]:
        """Build service overview section content."""
        status_color = get_status_color(service.status)

        content = [
            f"Name:         {service.name}",
            f"Status:       [{status_color}]{service.status.upper()}[/{status_color}]",
            f"Image:        {format_image_name(service.image)}",
            f"Replicas:     {service.ready_replicas}/{service.replicas}",
        ]

        if service.current_usage:
            if service.current_usage.cpu:
                content.append(f"CPU Usage:    {service.current_usage.cpu}")
            if service.current_usage.memory:
                content.append(f"Memory Usage: {service.current_usage.memory}")

        if service.last_checked:
            content.append(
                f"Last Checked: {service.last_checked.strftime('%Y-%m-%d %H:%M:%S')}"
            )

        return content

    def _build_ports_content(self, ports: list) -> list[str]:
        """Build service ports section content."""
        if not ports:
            return ["No ports exposed"]

        return [f"● {port}" for port in ports]

    def _build_resources_content(self, resources: Resources) -> list[str]:
        """Build service resources section content."""
        content = []
        limits = resources.limits
        requests = resources.requests

        cpu_line = "CPU:      "
        if requests and requests.cpu:
            cpu_line += f"Requests: {requests.cpu}"
        if limits and limits.cpu:
            if requests and requests.cpu:
                cpu_line += f", Limits: {limits.cpu}"
            else:
                cpu_line += f"Limits: {limits.cpu}"
        if cpu_line == "CPU:      ":
            cpu_line += "Not specified"
        content.append(cpu_line)

        mem_line = "Memory:   "
        if requests and requests.memory:
            mem_line += f"Requests: {requests.memory}"
        if limits and limits.memory:
            if requests and requests.memory:
                mem_line += f", Limits: {limits.memory}"
            else:
                mem_line += f"Limits: {limits.memory}"
        if mem_line == "Memory:   ":
            mem_line += "Not specified"
        content.append(mem_line)

        return content

    def _build_health_content(self, healthcheck: HealthCheckValues) -> list[str]:
        """Build service health check section content."""
        if not healthcheck or not healthcheck.enabled:
            return ["Disabled"]

        content = []

        if healthcheck.livenessProbe:
            probe = healthcheck.livenessProbe
            probe_type = (
                "HTTP" if probe.http_get else "TCP" if probe.tcp_socket else "Exec"
            )
            content.append(f"Liveness:  {probe_type} check")

        if healthcheck.readinessProbe:
            probe = healthcheck.readinessProbe
            probe_type = (
                "HTTP" if probe.http_get else "TCP" if probe.tcp_socket else "Exec"
            )
            content.append(f"Readiness: {probe_type} check")

        return content if content else ["Not configured"]

    def _build_autoscaling_content(self, hpa: HPAValues | None) -> list[str]:
        """Build service autoscaling section content."""
        if not hpa or not hpa.enabled:
            return ["Auto-scaling disabled"]

        content = [
            "Status:       [green]Enabled[/green]",
            f"Min Replicas: {hpa.minReplicas}",
            f"Max Replicas: {hpa.maxReplicas}",
        ]

        if hpa.metrics:
            content.append(
                f"Target CPU:   {hpa.metrics[0].resource.target.averageUtilization}%"
            )
            content.append(
                f"Target Mem:   {hpa.metrics[1].resource.target.averageUtilization}%"
            )

        return content

    def _create_section(self, title: str, content: list[str]) -> None:
        """Create a section with content in the parent container."""
        if not self._scroll:
            return
        section = SectionContainer(title)
        self._scroll.mount(section)

        text_content = "\n".join(content).strip()
        widget = Static(text_content, markup=True)
        section.mount(widget)

    def _create_pods_table(
        self, deployment_id: str | None, service_name: str | None
    ) -> PodTable:
        """Create a data table for instances."""
        if not self._scroll:
            return None
        table = PodTable(
            deployment_id=deployment_id or "",
            service_name=service_name or "",
            show_header=True,
            zebra_stripes=True,
            cursor_type="row",
        )
        self._scroll.mount(table)
        return table

    async def _connect_service_stream(self) -> None:
        """Connect to SSE stream for real-time service updates."""
        if not self.deployment_id or not self.service_name:
            return

        max_reconnect_attempts = 5
        reconnect_delay = 3  # seconds

        for attempt in range(max_reconnect_attempts):
            try:

                def on_update(data: ServiceStatus) -> None:
                    """Handle incoming SSE events."""
                    # Use call_later to ensure UI updates happen on the main thread
                    self.app.call_later(self.update_overview, data)

                    if data.pods:
                        self.app.call_later(self.update_pods_table, data.pods)

                def on_error(error: Exception) -> None:
                    """Handle SSE stream errors."""
                    logger.warning(
                        f"SSE stream error for service {self.service_name}: {error}"
                    )

                await api.status.stream_service_status(
                    deployment_id=self.deployment_id,
                    service_name=self.service_name,
                    on_update=on_update,
                    on_error=on_error,
                )

                break

            except Exception as e:
                logger.error(
                    f"SSE stream connection failed for service {self.service_name} "
                    f"(attempt {attempt + 1}/{max_reconnect_attempts}): {e}"
                )

                # Only reconnect if still mounted and not on last attempt
                if attempt < max_reconnect_attempts - 1 and self.is_mounted:
                    logger.info(f"Reconnecting to SSE stream in {reconnect_delay}s...")
                    await asyncio.sleep(reconnect_delay)
                else:
                    logger.error(
                        f"Failed to maintain SSE connection for service {self.service_name}"
                    )
                    break
