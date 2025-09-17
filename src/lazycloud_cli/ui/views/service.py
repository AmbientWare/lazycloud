from datetime import datetime
from typing import Any

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, VerticalScroll
from textual.reactive import reactive
from textual.widgets import DataTable, Footer, Header, Label, Rule, Static

from lazycloud_cli.api import api
from shared.models.statuses import PodStatus, ServiceStatus


class ServiceView:
    """Compatibility wrapper for the service view."""

    def __init__(self, console):
        self.console = console

    def show_error(self, message: str):
        """Show error message."""
        if self.console:
            self.console.print(f"[red]✗ {message}[/red]")

    def show_info(self, message: str):
        """Show info message."""
        if self.console:
            self.console.print(f"[blue]ℹ {message}[/blue]")


class StatusCard(Container):
    """A card-like container for status information."""

    DEFAULT_CSS = """
    StatusCard {
        border: round $primary;
        padding: 1;
        margin: 1;
        height: auto;
    }
    
    StatusCard > Label {
        margin-bottom: 1;
        text-style: bold;
    }
    """

    def __init__(self, title: str, *children, **kwargs):
        super().__init__(*children, **kwargs)
        self.title = title

    def compose(self) -> ComposeResult:
        yield Label(self.title)
        yield from self.children


class ServiceStatusApp(App):
    """Textual app for displaying scrollable service status."""

    CSS = """
    #main-container {
        background: $surface;
        height: 100%;
        width: 100%;
        overflow-y: auto;
    }
    
    #connection-status {
        dock: bottom;
        height: 1;
        background: $surface;
        color: $text-muted;
        text-align: right;
        padding: 0 2;
    }
    
    DataTable {
        height: auto;
        margin: 0 1;
    }
    
    .info-row {
        layout: horizontal;
        height: 1;
        margin: 0 1;
    }
    
    .info-label {
        width: 20;
        text-style: bold;
    }
    
    .section-title {
        margin: 1;
        text-style: bold;
        color: $primary;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("ctrl+c", "quit", "Quit"),
        ("a", "auto_scroll", "Auto-scroll"),
        ("g", "scroll_home", "Top"),
        ("G", "scroll_end", "Bottom"),
    ]

    # Reactive attributes
    auto_scroll = reactive(True)

    def __init__(
        self,
        deployment_id: str,
        service_name: str,
        deployment_info: dict[str, Any],
    ):
        super().__init__()
        self.deployment_id = deployment_id
        self.service_name = service_name
        self.deployment_info = deployment_info
        self.ws_client = None
        self._current_status: dict[str, Any] | None = None
        self._error_message: str | None = None

    def compose(self) -> ComposeResult:
        """Create child widgets."""
        yield Header(show_clock=True)

        with VerticalScroll(id="main-container"):
            # Service Overview
            yield Label("📦 Service Overview", classes="section-title")
            with Container(id="overview-section"):
                yield Horizontal(
                    Label("Service:", classes="info-label"),
                    Label(self.service_name, id="service-name"),
                    classes="info-row",
                )
                yield Horizontal(
                    Label("Deployment:", classes="info-label"),
                    Label(
                        self.deployment_info.get("name", "Unknown"),
                        id="deployment-name",
                    ),
                    classes="info-row",
                )
                yield Horizontal(
                    Label("Image:", classes="info-label"),
                    Label("Loading...", id="image-name"),
                    classes="info-row",
                )
                yield Horizontal(
                    Label("Status:", classes="info-label"),
                    Label("Loading...", id="service-status"),
                    classes="info-row",
                )

            yield Rule()

            # Replicas & Scaling
            yield Label("🔄 Replicas & Scaling", classes="section-title")
            with Container(id="replicas-section"):
                yield Horizontal(
                    Label("Current Replicas:", classes="info-label"),
                    Label("Loading...", id="replicas"),
                    classes="info-row",
                )
                yield Horizontal(
                    Label("Auto-scaling:", classes="info-label"),
                    Label("Loading...", id="autoscaling"),
                    classes="info-row",
                )
                yield Horizontal(
                    Label("Total Restarts:", classes="info-label"),
                    Label("Loading...", id="restarts"),
                    classes="info-row",
                )

            yield Rule()

            # Networking
            yield Label("🌐 Networking", classes="section-title")
            yield DataTable(id="ports-table", show_header=True)

            yield Rule()

            # Health Checks
            yield Label("❤️ Health Checks", classes="section-title")
            yield Container(
                Label("Loading...", id="health-checks"),
                id="health-section",
            )

            yield Rule()

            # Resources
            yield Label("💻 Resources", classes="section-title")
            yield DataTable(id="resources-table", show_header=True)

            yield Rule()

            # Instances
            yield Label("🔍 Instances", classes="section-title", id="instances-title")
            yield DataTable(id="instances-table", show_header=True)

        yield Static("", id="connection-status")
        yield Footer()

    async def on_mount(self) -> None:
        """Initialize tables and start WebSocket connection."""
        # Initialize tables
        ports_table = self.query_one("#ports-table", DataTable)
        ports_table.add_columns("Port", "Protocol")

        resources_table = self.query_one("#resources-table", DataTable)
        resources_table.add_columns("Resource", "Requests", "Limits", "Current Usage")

        instances_table = self.query_one("#instances-table", DataTable)
        instances_table.add_columns(
            "Instance", "Ready", "Status", "CPU", "Memory", "Restarts", "Age"
        )

        # Initial status
        self._update_connection_status("Connecting...")

        # Start WebSocket connection
        self.run_worker(self._connect_websocket(), exclusive=True)

    def _update_display(self) -> None:
        """Update the display with current status."""
        if not self._current_status:
            return

        # Get service data
        service_data = self._get_service_data()
        if not service_data:
            self.query_one("#service-status", Label).update("Service not found")
            self._update_connection_status("Service not found")
            return

        # Update basic info
        self.query_one("#image-name", Label).update(service_data.get("image", "N/A"))
        self.query_one("#service-status", Label).update(
            service_data.get("status", "Unknown").upper()
        )

        # Update replicas
        ready = service_data.get("ready_replicas", 0)
        total = service_data.get("replicas", 1)
        self.query_one("#replicas", Label).update(f"{ready}/{total}")

        # Update auto-scaling
        hpa = service_data.get("hpa", {})
        if hpa and hpa.get("enabled"):
            min_replicas = hpa.get("min_replicas", 1)
            max_replicas = hpa.get("max_replicas", 10)
            self.query_one("#autoscaling", Label).update(
                f"Enabled ({min_replicas}-{max_replicas})"
            )
        else:
            self.query_one("#autoscaling", Label).update("Disabled")

        # Update restarts
        restart_count = service_data.get("restart_count", 0)
        self.query_one("#restarts", Label).update(str(restart_count))

        # Update ports
        self._update_ports_table(service_data.get("ports", []))

        # Update health checks
        healthcheck = service_data.get("healthcheck", {})
        if healthcheck:
            health_text = "Configured"
        else:
            health_text = "No health checks configured"
        self.query_one("#health-checks", Label).update(health_text)

        # Update resources
        self._update_resources_table(service_data)

        # Update instances
        pods = self._get_pods_data()
        self._update_instances_table(pods)
        self.query_one("#instances-title", Label).update(f"🔍 Instances ({len(pods)})")

        # Update connection status
        self._update_connection_status("Live updates active")

    def _get_service_data(self) -> dict[str, Any] | None:
        """Extract service data from current status."""
        if not self._current_status:
            return None

        if "service" in self._current_status:
            return self._current_status["service"]
        else:
            # Old format compatibility
            services_data = self._current_status.get("services", {})
            if isinstance(services_data, dict):
                return services_data.get(self.service_name)
        return None

    def _get_pods_data(self) -> list:
        """Extract pods data from current status."""
        if not self._current_status:
            return []

        if "pods" in self._current_status and isinstance(
            self._current_status["pods"], list
        ):
            return self._current_status["pods"]
        else:
            # Old format compatibility
            pods_data = self._current_status.get("pods", {})
            if isinstance(pods_data, dict):
                return pods_data.get(self.service_name, [])
        return []

    def _update_ports_table(self, ports: list) -> None:
        """Update ports table."""
        table = self.query_one("#ports-table", DataTable)
        table.clear()

        for port in ports:
            if isinstance(port, dict):
                port_str = port.get("port", "")
                protocol = port.get("protocol", "TCP")
            else:
                # Handle string format "8000:8000/TCP"
                if "/" in str(port):
                    port_str, protocol = str(port).rsplit("/", 1)
                else:
                    port_str = str(port)
                    protocol = "TCP"
            table.add_row(port_str, protocol)

    def _update_resources_table(self, service: ServiceStatus) -> None:
        """Update resources table."""
        table = self.query_one("#resources-table", DataTable)
        table.clear()

        resources = service.resources
        limits = resources.limits
        requests = resources.requests
        current_usage = service.current_usage

        # CPU
        cpu_requests = requests.cpu
        cpu_limits = limits.cpu
        cpu_usage = current_usage.cpu
        table.add_row("CPU", cpu_requests, cpu_limits, cpu_usage)

        # Memory
        mem_requests = requests.memory
        mem_limits = limits.memory
        mem_usage = current_usage.memory
        table.add_row("Memory", mem_requests, mem_limits, mem_usage)

    def _update_instances_table(self, pods: list[PodStatus]) -> None:
        """Update instances table."""
        table = self.query_one("#instances-table", DataTable)
        table.clear()

        for pod in pods:
            name = pod.name[-16:]  # Truncate long names
            ready = pod.ready_containers
            total = pod.total_containers
            ready_str = f"{ready}/{total}"
            status = pod.phase
            cpu = pod.cpu_usage
            memory = pod.memory_usage
            restarts = str(pod.restart_count)
            age = pod.age

            table.add_row(name, ready_str, status, cpu, memory, restarts, age)

    def _update_connection_status(self, message: str) -> None:
        """Update connection status bar."""
        current_time = datetime.now().strftime("%H:%M:%S")

        if self._error_message:
            status = f"{self._error_message} • 🕐 {current_time}"
        else:
            scroll_status = "Auto-scrolling" if self.auto_scroll else "Manual scroll"
            status = f"{message} • {scroll_status} • 🕐 {current_time}"

        status_widget = self.query_one("#connection-status", Static)
        status_widget.update(status)

    def _on_status_update(self, data: dict[str, Any]) -> None:
        """Handle status update from WebSocket."""
        self._current_status = data
        # WebSocket callbacks are already in the app's thread, so call directly
        self._update_display()

    def _on_error(self, error: Exception) -> None:
        """Handle WebSocket errors."""
        self._error_message = f"❌ {error}"
        # WebSocket callbacks are already in the app's thread, so call directly
        self._update_display()

    async def _connect_websocket(self) -> None:
        """Connect to WebSocket for real-time updates."""
        try:
            self.ws_client = await api.status.stream_deployment_status(
                deployment_id=self.deployment_id,
                on_update=self._on_status_update,
                on_error=self._on_error,
                service_name=self.service_name,
            )
        except Exception as e:
            self._on_error(e)

    def action_auto_scroll(self) -> None:
        """Toggle auto-scroll."""
        self.auto_scroll = not self.auto_scroll
        self._update_connection_status("Live updates active")

    def action_scroll_home(self) -> None:
        """Scroll to top."""
        self.auto_scroll = False
        container = self.query_one("#main-container", VerticalScroll)
        container.scroll_home(animate=False)
        self._update_connection_status("Live updates active")

    def action_scroll_end(self) -> None:
        """Scroll to bottom."""
        self.auto_scroll = True
        container = self.query_one("#main-container", VerticalScroll)
        container.scroll_end(animate=False)
        self._update_connection_status("Live updates active")

    async def on_unmount(self) -> None:
        """Clean up when app unmounts."""
        if self.ws_client:
            await self.ws_client.disconnect()
