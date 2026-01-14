import asyncio

from loguru import logger
from models.helm import HealthCheckValues, HPAValues
from models.k8s import WorkloadType
from models.statuses import ServiceStatus, StatusPhase
from textual.app import ComposeResult
from textual.containers import VerticalScroll
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
from cli.ui.textual.messages import ServiceStatusUpdated
from cli.ui.textual.theme import Icons, Symbols
from cli.utils.utils import format_cpu, format_image_name, format_memory


class ServiceDetailsContainer(Widget):
    """Handles service-specific UI rendering and updates."""

    def __init__(
        self,
        deployment_id: str,
        service_name: str,
        service_status: ServiceStatus,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.deployment_id = deployment_id
        self.service_name = service_name
        self.service_status = service_status
        self._overview_widget: Static | None = None
        self._resources_table: ResourcesTable | None = None
        self._pods_table: PodTable | None = None
        self._stream_task: Worker | None = None
        self._scroll: VerticalScroll | None = None

    def compose(self) -> ComposeResult:
        """Create the initial UI structure."""
        self._scroll = VerticalScroll(id="service-details-scroll")
        yield self._scroll

    def on_mount(self) -> None:
        """Render content and start streaming when mounted."""
        logger.debug(f"ServiceDetailsContainer.on_mount for {self.service_name}")
        self._render_sections(self.service_status)
        self._start_stream()

    async def on_unmount(self) -> None:
        """Clean up when unmounting."""
        logger.debug(f"ServiceDetailsContainer.on_unmount for {self.service_name}")
        await self._stop_stream()

    def _start_stream(self) -> None:
        """Start SSE stream for real-time updates."""
        logger.debug(f"ServiceDetailsContainer._start_stream for {self.service_name}")
        self._stream_task = self.run_worker(
            self._connect_service_stream(),
            exclusive=True,
        )
        logger.debug(
            f"ServiceDetailsContainer._start_stream worker created: {self._stream_task}"
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

    def _render_sections(self, service: ServiceStatus) -> None:
        """Render all sections with the service data."""
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

    def _update_from_stream(self, service: ServiceStatus) -> None:
        """Update UI from stream data."""
        if not self.is_mounted:
            return

        # Update cached state in parent containers
        self.post_message(
            ServiceStatusUpdated(
                deployment_id=self.deployment_id,
                service_status=service,
            )
        )

        if self._overview_widget:
            overview_content = self._build_overview_content(service)
            self._overview_widget.update("\n".join(overview_content).strip())

        if self._pods_table and service.pods:
            self._pods_table.update_pods(service.pods)

    def action_focus_instances(self) -> None:
        """Focus the instances table."""
        if self._pods_table:
            self._pods_table.focus()

    def _build_overview_content(self, service: ServiceStatus) -> list[str]:
        """Build service overview section content."""
        deploy_phase = service.get_deploy_phase()
        status_color = get_status_color(deploy_phase)

        if service.workload_type == WorkloadType.JOB:
            if deploy_phase == StatusPhase.EXITED:
                completion_text = "Completion:   [green]Completed[/green]"
            elif deploy_phase == StatusPhase.ERROR:
                completion_text = "Completion:   [red]Failed[/red]"
            elif deploy_phase == StatusPhase.RUNNING:
                completion_text = "Completion:   [yellow]Running[/yellow]"
            else:
                completion_text = "Completion:   [dim]Pending[/dim]"
        else:
            completion_text = (
                f"Replicas:     {service.ready_replicas}/{service.replicas}"
            )

        content = [
            f"Name:         {service.name}",
            f"Status:       [{status_color}]{deploy_phase.upper()}[/{status_color}]",
            f"Image:        {format_image_name(service.image)}",
            completion_text,
        ]

        if service.endpoint:
            endpoint_url = (
                service.endpoint
                if service.endpoint.startswith(("http://", "https://"))
                else f"https://{service.endpoint}"
            )
            content.append(
                f'Endpoint:     [link="{endpoint_url}"][cyan]{service.endpoint}[/cyan][/link]'
            )

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

        if service.pods:
            error_pods = [
                pod
                for pod in service.pods
                if pod.phase.value in ["Error", "Pending"] and pod.reason
            ]
            if error_pods:
                content.append("")
                content.append("[red]Pod Errors:[/red]")
                for pod in error_pods:
                    error_msg = f"  • {pod.name}: {pod.reason}"
                    if pod.message:
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

    def _create_pods_table(self, deployment_id: str, service_name: str) -> PodTable:
        """Create a data table for instances."""
        if not self._scroll:
            return None
        table = PodTable(
            deployment_id=deployment_id,
            service_name=service_name,
            show_header=True,
            zebra_stripes=True,
            cursor_type="row",
        )
        self._scroll.mount(table)
        return table

    async def _connect_service_stream(self) -> None:
        """Connect to SSE stream for real-time service updates."""
        logger.debug(
            f"ServiceDetailsContainer._connect_service_stream ENTERED for {self.service_name}"
        )
        max_reconnect_attempts = 5
        reconnect_delay = 3

        for attempt in range(max_reconnect_attempts):
            try:
                logger.debug(
                    "ServiceDetailsContainer calling api.status.stream_service_status"
                )
                await api.status.stream_service_status(
                    deployment_id=self.deployment_id,
                    service_name=self.service_name,
                    on_update=lambda data: self.app.call_later(
                        self._update_from_stream, data
                    ),
                    on_error=lambda e: logger.warning(
                        f"SSE stream error for {self.service_name}: {e}"
                    ),
                )
                break

            except Exception as e:
                logger.error(
                    f"SSE connection failed for {self.service_name} "
                    f"(attempt {attempt + 1}/{max_reconnect_attempts}): {e}"
                )

                if attempt < max_reconnect_attempts - 1 and self.is_mounted:
                    await asyncio.sleep(reconnect_delay)
                else:
                    break
