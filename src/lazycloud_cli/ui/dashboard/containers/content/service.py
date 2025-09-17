from typing import Optional

from textual.containers import Container
from textual.widgets import DataTable, RichLog, Static
from textual.worker import Worker

from lazycloud_cli.api.logs import logs_api
from lazycloud_cli.api.status import status_api
from lazycloud_cli.ui.dashboard.containers.common import SectionContainer
from shared.models.helm import HealthCheckValues, HPAValues
from shared.models.k8s import Resources
from shared.models.statuses import KubernetesPhase, PodStatus, ServiceStatus


class ServiceView:
    """Handles service-specific UI rendering and updates."""

    def __init__(self, parent_container: Container):
        self.parent = parent_container
        self._overview_widget: Optional[Static] = None
        self._pods_table: Optional[DataTable] = None
        self._logs_widget: Optional[RichLog] = None
        self._logs_client = None
        self._ws_task: Optional[Worker] = None
        self.ws_client = status_api
        self.current_deployment_id: Optional[str] = None
        self.current_service_name: Optional[str] = None

    async def render(
        self,
        service: ServiceStatus,
        deployment_id: str,
        run_worker_fn,
    ) -> None:
        """Render service details in the parent container"""
        self.current_deployment_id = deployment_id
        self.current_service_name = service.name

        # Service Overview
        overview_content = self._build_overview_content(service)
        overview_section = SectionContainer("📦 Service Overview")
        self.parent.mount(overview_section)
        self._overview_widget = Static("\n".join(overview_content).strip(), markup=True)
        overview_section.mount(self._overview_widget)

        # Ports
        if service.ports:
            ports_content = self._build_ports_content(service.ports)
            self._create_section("🌐 Network Ports", ports_content)

        # Resources
        if service.resources and (
            service.resources.limits or service.resources.requests
        ):
            resources_content = self._build_resources_content(service.resources)
            self._create_section("💻 Resource Configuration", resources_content)

        # Health Checks
        if service.healthcheck:
            health_content = self._build_health_content(service.healthcheck)
            self._create_section("❤️ Health Checks", health_content)

        # Auto-scaling
        autoscaling_content = self._build_autoscaling_content(service.hpa)
        self._create_section("🔄 Auto-scaling", autoscaling_content)

        # Pods/Instances table
        self._pods_table = self._create_pods_table()
        if service.pods:
            self._update_pods_table(service.pods)

        # Service Logs section
        logs_section = SectionContainer("📜 Service Logs")
        self.parent.mount(logs_section)

        # Create RichLog widget for logs display
        self._logs_widget = RichLog(highlight=True, markup=True)
        self._logs_widget.styles.height = 15  # Fixed height for logs
        self._logs_widget.styles.min_height = 10
        logs_section.mount(self._logs_widget)

        # Add initial message
        self._logs_widget.write("[dim]Connecting to log stream...[/dim]")

        # Start WebSocket connection for real-time updates
        self._ws_task = run_worker_fn(self._connect_service_websocket())

        # Start logs streaming in background
        run_worker_fn(self._connect_logs_stream())

    def update_overview(self, service: ServiceStatus) -> None:
        """Update the overview widget with new service data."""
        if self._overview_widget:
            overview_content = self._build_overview_content(service)
            self._overview_widget.update("\n".join(overview_content).strip())

    def update_pods_table(self, pods: list[PodStatus]) -> None:
        """Update the pods table with new data."""
        if self._pods_table:
            self._update_pods_table(pods)

    async def cleanup(self) -> None:
        """Clean up WebSocket connections and tasks."""
        if self._ws_task:
            self._ws_task.cancel()
            self._ws_task = None

        if self.ws_client:
            await self.ws_client.disconnect()
            self.ws_client = None

        if self._logs_client:
            await self._logs_client.disconnect()
            self._logs_client = None

    def _build_overview_content(self, service: ServiceStatus) -> list[str]:
        """Build service overview section content."""
        status_color = self._get_status_color(service.status)

        content = [
            f"Name:         {service.name}",
            f"Status:       [{status_color}]{service.status.upper()}[/{status_color}]",
            f"Image:        {service.image}",
            f"Replicas:     {service.ready_replicas}/{service.replicas}",
        ]

        # Add current usage if available
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
        content = []
        if healthcheck.test:
            content.append(
                f"Test:     {' '.join(healthcheck.test) if isinstance(healthcheck.test, list) else healthcheck.test}"
            )
        if healthcheck.interval:
            content.append(f"Interval: {healthcheck.interval}")
        if healthcheck.timeout:
            content.append(f"Timeout:  {healthcheck.timeout}")
        if healthcheck.retries:
            content.append(f"Retries:  {healthcheck.retries}")

        return content

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

    def _create_pods_table(self) -> DataTable:
        """Create a data table for instances."""
        section = SectionContainer("🔍 Instances")
        self.parent.mount(section)

        table = DataTable(show_header=True, zebra_stripes=True)
        table.add_columns(
            "Instance Name", "Status", "Ready", "CPU", "Memory", "Restarts", "Age"
        )
        section.mount(table)

        return table

    def _update_pods_table(self, pods: list[PodStatus]) -> None:
        """Update the instances table with data."""
        if not self._pods_table:
            return

        self._pods_table.clear()

        for pod in pods:
            status_color = self._get_status_color(pod.phase)
            status_text = f"[{status_color}]{pod.phase.value}[/{status_color}]"

            ready = f"{pod.ready_containers}/{pod.total_containers}"

            cpu = pod.cpu_usage or "N/A"
            memory = pod.memory_usage or "N/A"

            restarts = str(pod.restart_count) if pod.restart_count > 0 else "0"

            age = pod.age or "Unknown"

            name = pod.name
            if len(name) > 30:
                name = name[:27] + "..."

            self._pods_table.add_row(
                name, status_text, ready, cpu, memory, restarts, age
            )

    def _get_status_color(self, status: KubernetesPhase) -> str:
        """Get color for status display."""
        if status == KubernetesPhase.RUNNING:
            return "green"
        elif status in (KubernetesPhase.PENDING, KubernetesPhase.PARTIALLY_RUNNING):
            return "yellow"
        elif status == KubernetesPhase.STOPPED:
            return "dim"
        else:
            return "red"

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

            def on_error(error: Exception) -> None:
                """Handle WebSocket errors."""
                pass  # Silently ignore errors for now

            await self.ws_client.connect_service(
                deployment_id=self.current_deployment_id,
                service_name=self.current_service_name,
                on_message=on_message,
                on_error=on_error,
            )

        except Exception:
            pass  # Silently handle connection errors

    async def _connect_logs_stream(self) -> None:
        """Connect to the logs WebSocket stream."""
        if not self.current_deployment_id or not self.current_service_name:
            return

        def on_log_message(data: dict) -> None:
            """Handle incoming log messages."""
            if self._logs_widget:
                log_line = data.get("line", "")

                if log_line:
                    self._logs_widget.write(log_line)

        def on_log_error(error: Exception) -> None:
            """Handle log stream errors."""
            if self._logs_widget:
                self._logs_widget.write(f"[red]Log stream error: {str(error)}[/red]")

        # Clear the "connecting" message
        if self._logs_widget:
            self._logs_widget.clear()
            self._logs_widget.write("[green]Connected to log stream[/green]\n")

        # Connect to logs stream
        self._logs_client = logs_api
        await self._logs_client.stream_logs(
            deployment_id=self.current_deployment_id,
            service_name=self.current_service_name,
            tail=100,  # Get last 100 lines
            on_message=on_log_message,
            on_error=on_log_error,
        )
