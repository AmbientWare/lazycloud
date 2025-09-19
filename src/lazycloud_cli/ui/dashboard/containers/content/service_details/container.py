from textual.widgets import Static
from textual.worker import Worker

from lazycloud_cli.api import api
from lazycloud_cli.ui.dashboard.components import Container
from lazycloud_cli.ui.dashboard.components.section import SectionContainer
from lazycloud_cli.ui.dashboard.containers.content.service_details.pods_table import (
    PodTable,
)
from lazycloud_cli.ui.dashboard.containers.content.utils import get_status_color
from shared.models.helm import HealthCheckValues, HPAValues
from shared.models.k8s import Resources
from shared.models.statuses import PodStatus, ServiceStatus


class ServiceDetailsContainer:
    """Handles service-specific UI rendering and updates."""

    def __init__(self, parent_container: Container):
        self.parent = parent_container
        self._overview_widget: Static | None = None
        self._pods_table: PodTable | None = None
        self._ws_task: Worker | None = None
        self.current_deployment_id: str | None = None
        self.current_service_name: str | None = None

    async def render(
        self,
        service: ServiceStatus,
        deployment_id: str,
        run_worker_fn,
    ) -> None:
        """Render service details in the parent container"""
        self.current_deployment_id = deployment_id
        self.current_service_name = service.name
        self.run_worker_fn = run_worker_fn
        self._pods = []

        overview_content = self._build_overview_content(service)
        overview_section = SectionContainer("📦 Overview")
        self.parent.mount(overview_section)
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

        self._pods_table = self._create_pods_table(deployment_id, service.name)
        if service.pods:
            self._pods_table.update_pods(service.pods)

        self._ws_task = run_worker_fn(self._connect_service_websocket())

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
        """Clean up WebSocket connections and tasks."""
        if self._ws_task:
            self._ws_task.cancel()
            self._ws_task = None

        if api.status:
            await api.status.disconnect()

    def _build_overview_content(self, service: ServiceStatus) -> list[str]:
        """Build service overview section content."""
        status_color = get_status_color(service.status)

        content = [
            f"Name:         {service.name}",
            f"Status:       [{status_color}]{service.status.upper()}[/{status_color}]",
            f"Image:        {service.image}",
            f"Replicas:     {service.ready_replicas}/{service.replicas}",
        ]

        if service.current_usage:
            if service.current_usage.cpu:
                content.append(f"CPU Usage:    {service.current_usage.cpu}")
            if service.current_usage.memory:
                content.append(f"Memory Usage: {service.current_usage.memory}")

        return content

    def _build_ports_content(self, ports: list) -> list[str]:
        """Build service ports section content."""
        if not ports:
            return ["No ports exposed"]

        content = []
        for port in ports:
            port_str = (
                f"● {port.get('port', 'unknown')} ({port.get('protocol', 'TCP')})"
            )
            content.append(port_str)
        return content

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
        section = SectionContainer(title)
        self.parent.mount(section)

        text_content = "\n".join(content).strip()
        widget = Static(text_content, markup=True)
        section.mount(widget)

    def _create_pods_table(self, deployment_id: str, service_name: str) -> PodTable:
        """Create a data table for instances."""
        table = PodTable(
            deployment_id=deployment_id,
            service_name=service_name,
            show_header=True,
            zebra_stripes=True,
            cursor_type="row",
        )
        self.parent.mount(table)
        return table

    async def _connect_service_websocket(self) -> None:
        """Connect to WebSocket for real-time service updates."""
        if not self.current_deployment_id or not self.current_service_name:
            return

        try:

            def on_message(data: dict) -> None:
                """Handle incoming WebSocket messages."""
                if "service" in data:
                    service_data = data["service"]
                    if isinstance(service_data, dict):
                        service = ServiceStatus(**service_data)
                        self.update_overview(service)

                        if service.pods:
                            self.update_pods_table(service.pods)

            def on_error(_: Exception) -> None:
                """Handle WebSocket errors."""
                pass

            await api.status.stream_service_status(
                deployment_id=self.current_deployment_id,
                service_name=self.current_service_name,
                on_message=on_message,
                on_error=on_error,
            )

        except Exception:
            pass
