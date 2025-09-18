import asyncio

from textual.containers import Container
from textual.widgets import DataTable, RichLog, Static
from textual.worker import Worker

from lazycloud_cli.api import api
from lazycloud_cli.ui.dashboard.containers.common import SectionContainer
from lazycloud_cli.ui.dashboard.containers.content.utils import get_status_color
from shared.models.helm import HealthCheckValues, HPAValues
from shared.models.k8s import Resources
from shared.models.statuses import PodStatus, ServiceStatus


class PodTable(DataTable):
    """Custom DataTable for pod selection that handles its own events."""

    def __init__(self, service_view, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.service_view = service_view

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle row selection and notify the service view."""
        if event.row_key:
            pod_name = event.row_key.value
            self.service_view._on_pod_selected(pod_name)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Handle row highlight (cursor movement) and notify the service view."""
        if event.row_key:
            pod_name = event.row_key.value
            self.service_view._on_pod_selected(pod_name)


class ServiceView:
    """Handles service-specific UI rendering and updates."""

    def __init__(self, parent_container: Container):
        self.parent = parent_container
        self._overview_widget: Static | None = None
        self._pods_table: DataTable | None = None
        self._logs_widget: RichLog | None = None
        self._ws_task: Worker | None = None
        self.current_deployment_id: str | None = None
        self.current_service_name: str | None = None
        self.selected_pod_name: str | None = None
        self._logs_section: SectionContainer | None = None

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
        overview_section = SectionContainer("📦 Service Overview")
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

        self._pods_table = self._create_pods_table()
        if service.pods:
            self._update_pods_table(service.pods)

        self._logs_section = SectionContainer("📜 Service Logs (All Pods)")
        self.parent.mount(self._logs_section)

        self._logs_widget = RichLog(highlight=True, markup=True)
        self._logs_widget.styles.height = 15
        self._logs_widget.styles.min_height = 10
        self._logs_section.mount(self._logs_widget)

        self._logs_widget.write("[dim]Connecting to log stream for all pods...[/dim]")
        self._ws_task = run_worker_fn(self._connect_service_websocket())
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

        if api.status:
            await api.status.disconnect()

        if api.logs:
            await api.logs.disconnect()

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

        # Show liveness probe if configured
        if healthcheck.livenessProbe:
            probe = healthcheck.livenessProbe
            probe_type = "HTTP" if probe.http_get else "TCP" if probe.tcp_socket else "Exec"
            content.append(f"Liveness:  {probe_type} check")

        # Show readiness probe if configured
        if healthcheck.readinessProbe:
            probe = healthcheck.readinessProbe
            probe_type = "HTTP" if probe.http_get else "TCP" if probe.tcp_socket else "Exec"
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

    def _create_pods_table(self) -> DataTable:
        """Create a data table for instances."""
        section = SectionContainer("🔍 Instances (click to filter logs)")
        self.parent.mount(section)

        table = PodTable(self, show_header=True, zebra_stripes=True, cursor_type="row")
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

        self._pods = pods

        for pod in pods:
            status_color = get_status_color(pod.phase)
            status_text = f"[{status_color}]{pod.phase.value}[/{status_color}]"

            ready = f"{pod.ready_containers}/{pod.total_containers}"

            cpu = pod.cpu_usage or "N/A"
            memory = pod.memory_usage or "N/A"

            restarts = str(pod.restart_count) if pod.restart_count > 0 else "0"

            age = pod.age or "Unknown"

            display_name = pod.name
            if len(display_name) > 30:
                display_name = display_name[:27] + "..."

            self._pods_table.add_row(
                display_name,
                status_text,
                ready,
                cpu,
                memory,
                restarts,
                age,
                key=pod.name,
            )

    def _on_pod_selected(self, pod_name: str | None) -> None:
        """Handle pod selection from the table."""
        # Only restart if selection actually changed
        if self.selected_pod_name == pod_name:
            return

        self.selected_pod_name = pod_name

        if self._logs_section:
            if pod_name:
                display_name = (
                    pod_name if len(pod_name) <= 40 else pod_name[:37] + "..."
                )
                self._logs_section.border_title = (
                    f"📜 Service Logs (Pod: {display_name})"
                )
            else:
                self._logs_section.border_title = "📜 Service Logs (All Pods)"

        if self._logs_widget:
            self._logs_widget.clear()
            if pod_name:
                self._logs_widget.write(
                    f"[dim]Switching to logs for pod: {pod_name}...[/dim]"
                )
            else:
                self._logs_widget.write("[dim]Switching to logs for all pods...[/dim]")

        if self.run_worker_fn:
            self.run_worker_fn(self._restart_log_stream())

    async def _restart_log_stream(self) -> None:
        """Restart the log stream with current pod selection."""
        # Disconnect existing client if any
        if api.logs and api.logs.is_connected():
            await api.logs.disconnect()
            # Small delay to ensure clean disconnection
            await asyncio.sleep(0.5)

        # Create new connection with updated pod selection
        await self._connect_logs_stream()

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
                pass  # Silently ignore errors for now

            await api.status.stream_service_status(
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

        if self._logs_widget:
            self._logs_widget.clear()
            if self.selected_pod_name:
                self._logs_widget.write(
                    f"[green]Connected to log stream for pod: {self.selected_pod_name}[/green]\n"
                )
            else:
                self._logs_widget.write(
                    "[green]Connected to log stream for all pods[/green]\n"
                )

        await api.logs.stream_logs(
            deployment_id=self.current_deployment_id,
            service_name=self.current_service_name,
            tail=100,
            on_message=on_log_message,
            on_error=on_log_error,
            pod_name=self.selected_pod_name,  # Filter by selected pod if any
        )
