"""
Content container for the dashboard.
"""

from datetime import datetime

from textual.app import ComposeResult
from textual.containers import Container, VerticalScroll
from textual.widgets import DataTable, Static

from lazycloud_cli.api import api
from lazycloud_cli.ui.dashboard.theme import theme
from shared.models.helm import HealthCheckValues, HPAValues
from shared.models.k8s import Resources
from shared.models.statuses import KubernetesPhase, PodStatus, ServiceStatus


class SectionContainer(Container):
    """A bordered section container."""

    def __init__(self, title: str, **kwargs):
        super().__init__(**kwargs)
        self.border_title = title

    def on_mount(self) -> None:
        """Style the section container."""
        self.styles.border = ("round", theme.primary)
        self.styles.background = theme.background
        self.styles.padding = (0, 1)
        self.styles.height = "auto"


class ContentContainer(Container):
    """Main content container for displaying deployment details."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.current_deployment_id = None
        self.current_service_name = None
        self.display_mode = "deployment"  # "deployment" or "service"
        self.ws_client = None
        self._service_status_data = None
        self._ws_task = None
        self._overview_widget = None
        self._pods_table = None
        self._clock_timer = None
        self.base_title = "Details [3]"

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
        if self.ws_client:
            await self.ws_client.disconnect()
            self.ws_client = None

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

    def _get_status_color(self, status: KubernetesPhase) -> str:
        """Get color for status display."""
        if status == KubernetesPhase.RUNNING:
            return "green"
        elif status in (KubernetesPhase.PENDING, KubernetesPhase.PARTIALLY_RUNNING):
            return "yellow"
        elif status == KubernetesPhase.STOPPED:
            return "dim"
        elif status == KubernetesPhase.ERROR:
            return "red"
        return "red"

    def _create_section(
        self, title: str, content: list[str], parent: Container
    ) -> None:
        """Create and mount a section with content."""
        section = SectionContainer(title)
        widget = Static("\n".join(content).strip(), markup=True)
        parent.mount(section)
        section.mount(widget)

    def _build_overview_content(self, deployment_status) -> list[str]:
        """Build overview section content."""
        status_color = self._get_status_color(deployment_status.status)
        ready_color = "green" if deployment_status.ready else "red"

        return [
            f"Name:         {deployment_status.deployment_name}",
            f"Status:       [{status_color}]{deployment_status.status.upper()}[/{status_color}]",
            f"Ready:        [{ready_color}]{'✓ Yes' if deployment_status.ready else '✗ No'}[/{ready_color}]",
            f"Last Updated: {deployment_status.last_updated.strftime('%b %d, %H:%M')}",
        ]

    def _build_services_content(self, services: list[ServiceStatus]) -> list[str]:
        """Build services section content."""
        content = []

        # Service statistics
        stats = {
            "running": sum(1 for s in services if s.status == "running"),
            "pending": sum(1 for s in services if s.status == "pending"),
            "stopped": sum(1 for s in services if s.status == "stopped"),
            "error": sum(1 for s in services if s.status == "error"),
        }

        # Build stats line
        stats_parts = [f"Total: {len(services)}"]
        if stats["running"] > 0:
            stats_parts.append(f"[green]Running: {stats['running']}[/green]")
        if stats["pending"] > 0:
            stats_parts.append(f"[yellow]Pending: {stats['pending']}[/yellow]")
        if stats["stopped"] > 0:
            stats_parts.append(f"[dim]Stopped: {stats['stopped']}[/dim]")
        if stats["error"] > 0:
            stats_parts.append(f"[red]Error: {stats['error']}[/red]")

        content.append(" | ".join(stats_parts))
        content.append("")

        # Individual services
        for service in services:
            status_color = self._get_status_color(service.status)
            status_symbol = "●" if service.status == "running" else "○"
            content.append(
                f"[{status_color}]{status_symbol}[/{status_color}] {service.name} ({service.ready_replicas}/{service.replicas})"
            )
            content.append(f"  Image: {service.image}")

        return content

    async def update_content(self, deployment_id: str, deployment_name: str) -> None:
        """Update the content when a deployment is selected."""
        self.current_deployment_id = deployment_id
        self.base_title = f"Details [3] - {deployment_name}"
        self._update_title_with_clock()

        # Get the scroll container and clear it
        scroll = self.query_one("#content-scroll", VerticalScroll)
        scroll.remove_children()

        try:
            # Fetch deployment status
            status_response = api.deployments.get_deployment_status(deployment_id)
            deployment_status = status_response.status

            # Overview Section
            overview_content = self._build_overview_content(deployment_status)
            self._create_section("Overview", overview_content, scroll)

            # Services Section
            if deployment_status.services:
                services_content = self._build_services_content(
                    deployment_status.services
                )
                self._create_section("Services", services_content, scroll)

            # Volumes Section
            if deployment_status.volumes:
                volumes_content = [
                    f"● {name} - {status}"
                    for name, status in deployment_status.volumes.items()
                ]
                self._create_section("Volumes", volumes_content, scroll)

            # Networks Section
            if deployment_status.networks:
                networks_content = [
                    f"● {name} - {status}"
                    for name, status in deployment_status.networks.items()
                ]
                self._create_section("Networks", networks_content, scroll)

        except Exception as e:
            error_widget = Static(
                f"[red]Error loading deployment details: {str(e)}[/red]"
            )
            scroll.mount(error_widget)

    def _build_service_overview_content(self, service: ServiceStatus) -> list[str]:
        """Build service overview section content."""
        status_color = self._get_status_color(service.status)

        content = [
            f"Name:         {service.name}",
            f"Status:       [{status_color}]{service.status.upper()}[/{status_color}]",
            f"Image:        {service.image}",
            f"Replicas:     {service.ready_replicas}/{service.replicas}",
        ]

        # Add current usage if available
        current_usage = service.current_usage
        if current_usage:
            content.append(f"CPU Usage:    {current_usage.cpu}")
            content.append(f"Memory Usage: {current_usage.memory}")

        return content

    def _build_service_ports_content(self, ports: list) -> list[str]:
        """Build service ports section content."""
        if not ports:
            return ["No ports exposed"]

        content = []
        for port in ports:
            if isinstance(port, dict):
                port_str = f"● {port.port} ({port.protocol})"
            else:
                # Handle string format "8000:8000/TCP"
                if "/" in str(port):
                    port_str = f"● {port}"
                else:
                    port_str = f"● {port} (TCP)"
            content.append(port_str)
        return content

    def _build_service_resources_content(self, resources: Resources) -> list[str]:
        """Build service resources section content."""
        content = []
        limits = resources.limits
        requests = resources.requests

        # CPU
        cpu_line = "CPU:      "
        if requests.cpu:
            cpu_line += f"Requests: {requests.cpu}"
        if limits.cpu:
            if requests.cpu:
                cpu_line += f", Limits: {limits.cpu}"
            else:
                cpu_line += f"Limits: {limits.cpu}"
        if cpu_line == "CPU:      ":
            cpu_line += "Not specified"
        content.append(cpu_line)

        # Memory
        mem_line = "Memory:   "
        if requests.memory:
            mem_line += f"Requests: {requests.memory}"
        if limits.memory:
            if requests.memory:
                mem_line += f", Limits: {limits.memory}"
            else:
                mem_line += f"Limits: {limits.memory}"
        if mem_line == "Memory:   ":
            mem_line += "Not specified"
        content.append(mem_line)

        return content

    def _build_service_health_content(
        self, healthcheck: HealthCheckValues
    ) -> list[str]:
        """Build service health check section content."""
        if not healthcheck:
            return ["No health checks configured"]

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

        if not content:
            return ["Health check configured but no details available"]
        return content

    def _build_service_autoscaling_content(self, hpa: HPAValues) -> list[str]:
        """Build service autoscaling section content."""
        if not hpa or not hpa.enabled:
            return ["Auto-scaling disabled"]

        content = [
            "Status:       [green]Enabled[/green]",
            f"Min Replicas: {hpa.minReplicas}",
            f"Max Replicas: {hpa.maxReplicas}",
        ]

        if hpa.target_cpu:
            content.append(f"Target CPU:   {hpa.target_cpu}%")
        if hpa.target_memory:
            content.append(f"Target Mem:   {hpa.target_memory}%")

        return content

    def _create_pods_table(self, parent: Container) -> DataTable:
        """Create a data table for instances."""
        section = SectionContainer("🔍 Instances")
        parent.mount(section)

        table = DataTable(show_header=True, zebra_stripes=True)
        table.add_columns(
            "Instance Name", "Status", "Ready", "CPU", "Memory", "Restarts", "Age"
        )
        section.mount(table)

        return table

    def _update_pods_table(self, table: DataTable, pods: list[PodStatus]) -> None:
        """Update the instances table with data."""
        table.clear()

        for pod in pods:
            name = pod.name[-20:]  # Truncate long names
            phase = pod.phase
            ready = f"{pod.ready_containers}/{pod.total_containers}"
            cpu = pod.cpu_usage
            memory = pod.memory_usage
            restarts = str(pod.restart_count)
            age = pod.age

            # Color status based on phase
            if phase == KubernetesPhase.RUNNING:
                status = f"[green]{phase}[/green]"
            elif phase == KubernetesPhase.PENDING:
                status = f"[yellow]{phase}[/yellow]"
            else:
                status = f"[red]{phase}[/red]"

            table.add_row(name, status, ready, cpu, memory, restarts, age)

    async def update_service_content(
        self,
        deployment_id: str,
        service: ServiceStatus,
    ) -> None:
        """Update the content to show service details."""
        self.current_deployment_id = deployment_id
        self.current_service_name = service.name
        self.display_mode = "service"
        self.base_title = f"Service Details [3] - {service.name}"
        self._update_title_with_clock()

        # Cancel previous WebSocket task if any
        if self._ws_task:
            self._ws_task.cancel()
            self._ws_task = None

        # Disconnect previous WebSocket if any
        if self.ws_client:
            await self.ws_client.disconnect()
            self.ws_client = None

        # Get the scroll container and clear it
        scroll = self.query_one("#content-scroll", VerticalScroll)
        scroll.remove_children()

        try:
            # Service Overview
            overview_content = self._build_service_overview_content(service)
            overview_section = SectionContainer("📦 Service Overview")
            scroll.mount(overview_section)
            self._overview_widget = Static(
                "\n".join(overview_content).strip(), markup=True
            )
            overview_section.mount(self._overview_widget)

            # Ports
            ports = service.ports
            if ports:
                ports_content = self._build_service_ports_content(ports)
                self._create_section("🌐 Network Ports", ports_content, scroll)

            # Resources
            resources = service.resources
            if resources and (resources.limits or resources.requests):
                resources_content = self._build_service_resources_content(resources)
                self._create_section(
                    "💻 Resource Configuration", resources_content, scroll
                )

            # Health Checks
            healthcheck = service.healthcheck
            if healthcheck:
                health_content = self._build_service_health_content(healthcheck)
                self._create_section("❤️ Health Checks", health_content, scroll)

            # Auto-scaling
            hpa = service.hpa
            autoscaling_content = self._build_service_autoscaling_content(hpa)
            self._create_section("🔄 Auto-scaling", autoscaling_content, scroll)

            # Pods/Instances table
            self._pods_table = self._create_pods_table(scroll)

            # Initialize with empty data if we have pods
            initial_pods = service.pods
            if service.pods:
                self._update_pods_table(self._pods_table, initial_pods)

            # Mark widgets as ready for updates
            with open("./dashboard_debug.log", "a") as f:
                f.write(
                    f"DEBUG: UI widgets created - _overview_widget={self._overview_widget is not None}, _pods_table={self._pods_table is not None}\n"
                )

            # Start WebSocket connection for real-time updates
            # Store as a task so it keeps running
            self._ws_task = self.run_worker(self._connect_service_websocket())

        except Exception as e:
            error_widget = Static(f"[red]Error loading service details: {str(e)}[/red]")
            scroll.mount(error_widget)

    async def _connect_service_websocket(self) -> None:
        """Connect to WebSocket for real-time service updates."""
        if not self.current_deployment_id or not self.current_service_name:
            return

        try:
            # Start WebSocket connection (this will run until disconnected)
            self.ws_client = await api.status.stream_deployment_status(
                deployment_id=self.current_deployment_id,
                on_update=self._on_service_update,
                on_error=self._on_websocket_error,
                service_name=self.current_service_name,
            )
        except Exception as e:
            self._on_websocket_error(e)

    def _on_service_update(self, data: dict) -> None:
        """Handle service status update from WebSocket."""
        # Write to a debug file instead of stdout
        with open("./dashboard_debug.log", "a") as f:
            f.write(f"DEBUG: _on_service_update received data keys: {data.keys()}\n")
            if "service" in data:
                f.write(f"DEBUG: Service data keys: {data['service'].keys()}\n")
        self._service_status_data = data

        # Schedule UI updates on the main thread using call_from_thread
        try:
            self.call_from_thread(self._update_ui_with_service_data, data)
        except Exception as e:
            # Log but don't crash the WebSocket connection
            print(f"Error updating UI: {e}")
            import traceback

            traceback.print_exc()
            pass

    def _update_ui_with_service_data(self, service: ServiceStatus) -> None:
        """Update UI elements with service data (must be called from main thread)."""
        # Check if UI widgets exist before updating
        widgets_exist = (
            self._overview_widget is not None and self._pods_table is not None
        )

        if not widgets_exist:
            return

        # Update overview section with new metrics
        if self._overview_widget:
            # Use the service data directly from server
            overview_content = self._build_service_overview_content(service)
            self._overview_widget.update("\n".join(overview_content).strip())
            # Force refresh
            self._overview_widget.refresh()

        # Update pods table
        if self._pods_table:
            self._update_pods_table(self._pods_table, service.pods)

    def _on_websocket_error(self, error: Exception) -> None:
        """Handle WebSocket errors."""
        # Just log the error, don't update UI as it might be a temporary connection issue
        pass

    async def clear_content(self) -> None:
        """Clear the content area."""
        # Cancel WebSocket task if active
        if self._ws_task:
            self._ws_task.cancel()
            self._ws_task = None

        # Disconnect WebSocket if active
        if self.ws_client:
            await self.ws_client.disconnect()
            self.ws_client = None

        self.current_deployment_id = None
        self.current_service_name = None
        self.display_mode = "deployment"
        self.base_title = "Details [3]"
        self._update_title_with_clock()
        self._service_status_data = None

        scroll = self.query_one("#content-scroll", VerticalScroll)
        scroll.remove_children()

        # Reuse the same pattern as on_mount
        initial_section = SectionContainer("Overview")
        scroll.mount(initial_section)
        initial_section.mount(Static("Select a deployment to view details"))
