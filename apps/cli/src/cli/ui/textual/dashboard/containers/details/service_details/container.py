import asyncio

from loguru import logger
from models.helm import HealthCheckValues, HPAValues
from models.k8s import WorkloadType
from models.statuses import KubernetesPhase, PodStatus, ServiceStatus
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static
from textual.worker import Worker, WorkerCancelled

from cli.api import api
from cli.ui.colors import Colors
from cli.ui.textual.components import SectionContainer
from cli.ui.textual.dashboard.containers.details.service_details.pods_table import (
    PodTable,
)
from cli.ui.textual.dashboard.containers.details.service_details.resources_table import (
    ResourcesTable,
)
from cli.ui.textual.dashboard.containers.details.utils import get_status_color
from cli.ui.textual.theme import Icons, Symbols
from cli.utils.utils import format_cpu, format_image_name, format_memory


class ServiceDetailsContainer(Widget):
    """Handles service-specific UI rendering and updates."""

    # Reactive properties
    service_status: reactive[ServiceStatus | None] = reactive(None)
    deployment_id: reactive[str | None] = reactive(None)
    service_name: reactive[str | None] = reactive(None)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._overview_widget: Static | None = None
        self._resources_table: ResourcesTable | None = None
        self._pods_table: PodTable | None = None
        self._stream_task: Worker | None = None
        self._scroll: VerticalScroll | None = None
        self._initial_render_done = False
        self._stream_error: str | None = None

    def compose(self) -> ComposeResult:
        """Create the initial UI structure."""
        self._scroll = VerticalScroll(id="service-details-scroll")
        yield self._scroll

    def on_mount(self) -> None:
        """Start SSE stream connection when mounted."""
        self.call_after_refresh(self._show_loading)
        if self.deployment_id and self.service_name:
            self._start_stream()

    def _show_loading(self) -> None:
        """Show loading indicator after widget is fully mounted."""
        # Skip loading if we already have initial status to render
        if self.service_status and self._scroll and self._scroll.is_mounted:
            self._initial_render_done = True
            self._render_sections(self.service_status)
        else:
            self.loading = True

    def _show_error(self, message: str) -> None:
        """Show error message when status cannot be loaded."""
        if not self._scroll or not self._scroll.is_mounted:
            return

        self._scroll.remove_children()
        error_widget = Static(f"[yellow]{message}[/yellow]", markup=True)
        error_section = SectionContainer(
            f"{Icons.OVERVIEW} Status Unavailable", error_widget
        )
        self._scroll.mount(error_section)

    async def on_unmount(self) -> None:
        """Clean up when unmounting."""
        try:
            await self.cleanup()
        except Exception:
            pass  # Ignore cleanup errors during unmount

    async def watch_service_status(self, old_value, new_value) -> None:
        """React to service status changes."""
        if not new_value or not self.is_mounted:
            return

        # Render on initial set or updates
        if not self._initial_render_done:
            self._initial_render_done = True
            self._render_sections(new_value)
        elif old_value is not None:
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
        self.loading = False
        if not self._scroll:
            return
        self._scroll.remove_children()

        overview_content = self._build_overview_content(service)
        self._overview_widget = Static("\n".join(overview_content).strip(), markup=True)
        overview_section = SectionContainer(
            f"{Icons.OVERVIEW} Overview", self._overview_widget
        )
        self._scroll.mount(overview_section)

        if service.ports:
            ports_content = self._build_ports_content(service.ports)
            self._create_section(f"{Icons.NETWORKS} Network Ports", ports_content)

        if service.resources and (
            service.resources.limits or service.resources.requests
        ):
            self._resources_table = ResourcesTable()
            resources_section = SectionContainer(
                f"{Icons.SETTINGS} Resource Configuration", self._resources_table
            )
            self._scroll.mount(resources_section)
            self._resources_table.update_resources(service.resources)

        if service.healthcheck:
            health_content = self._build_health_content(service.healthcheck)
            self._create_section(f"{Icons.HEALTH} Health Checks", health_content)

        autoscaling_content = self._build_autoscaling_content(service.hpa)
        self._create_section(f"{Icons.RESTART} Auto-scaling", autoscaling_content)

        self._pods_table = self._create_pods_table(self.deployment_id, service.name)
        if service.pods:
            self._pods_table.update_pods(service.pods)

    def update_overview(self, service: ServiceStatus) -> None:
        """Update the overview widget with new service data."""
        if self._overview_widget is None:
            self._render_sections(service)
            return

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
        if self._stream_task:
            if not self._stream_task.is_finished:
                self._stream_task.cancel()
            try:
                await self._stream_task.wait()
            except (WorkerCancelled, Exception):
                pass  # Expected when cancelling the worker or if already finished
        self._stream_task = None
        self._initial_render_done = False
        self._stream_error = None

    def _build_overview_content(self, service: ServiceStatus) -> list[str]:
        """Build service overview section content."""
        status_color = get_status_color(service.status)

        # For Jobs, show completion status instead of replicas
        if service.workload_type == WorkloadType.JOB:
            if service.status == KubernetesPhase.STOPPED:
                completion_text = "Completion:   [green]Completed[/green]"
            elif service.status == KubernetesPhase.ERROR:
                completion_text = "Completion:   [red]Failed[/red]"
            elif service.status == KubernetesPhase.RUNNING:
                completion_text = "Completion:   [yellow]Running[/yellow]"
            else:
                completion_text = "Completion:   [dim]Pending[/dim]"
        else:
            completion_text = (
                f"Replicas:     {service.ready_replicas}/{service.replicas}"
            )

        content = [
            f"Name:         {service.name}",
            f"Status:       [{status_color}]{service.status.upper()}[/{status_color}]",
            f"Image:        {format_image_name(service.image)}",
            completion_text,
        ]

        if service.endpoint:
            content.append(f"Endpoint:     [cyan]{service.endpoint}[/cyan]")

        if service.current_usage:
            if service.current_usage.cpu:
                formatted_cpu = format_cpu(service.current_usage.cpu)
                content.append(f"Average CPU:  {formatted_cpu} cores")
            if service.current_usage.memory:
                formatted_mem = format_memory(service.current_usage.memory)
                content.append(f"Average Mem:  {formatted_mem}")

        if service.last_checked:
            content.append(
                f"Last Checked: {service.last_checked.strftime('%Y-%m-%d %H:%M:%S')}"
            )

        # Add pod error information if available
        if service.pods:
            error_pods = [
                pod
                for pod in service.pods
                if pod.phase.value in ["Error", "Pending"] and pod.reason
            ]
            if error_pods:
                content.append("")  # Spacing
                content.append("[red]Pod Errors:[/red]")
                for pod in error_pods:
                    error_msg = f"  • {pod.name}: {pod.reason}"
                    if pod.message:
                        # Truncate long messages
                        msg = (
                            pod.message[:80] + "..."
                            if len(pod.message) > 80
                            else pod.message
                        )
                        error_msg += f" - {msg}"
                    content.append(f"[red]{error_msg}[/red]")

        return content

    def _build_ports_content(self, ports: list) -> list[str]:
        """Build service ports section content."""
        if not ports:
            return ["No ports exposed"]

        return [f"{Symbols.BULLET} {port}" for port in ports]

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
            f"Status:       [{Colors.Hex.success}]Enabled[/{Colors.Hex.success}]",
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
        text_content = "\n".join(content).strip()
        widget = Static(text_content, markup=True)
        section = SectionContainer(title, widget)
        self._scroll.mount(section)

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
                    self._stream_error = None
                    self.app.call_later(self.update_overview, data)

                    if data.pods:
                        self.app.call_later(self.update_pods_table, data.pods)

                def on_error(error: Exception) -> None:
                    """Handle SSE stream errors."""
                    self._stream_error = str(error)
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
                self._stream_error = str(e)
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
                    # Show error if we don't have any status to display
                    if not self._initial_render_done and self.is_mounted:
                        self.app.call_later(
                            self._show_error,
                            f"Could not connect to status stream: {e}",
                        )
                    break
