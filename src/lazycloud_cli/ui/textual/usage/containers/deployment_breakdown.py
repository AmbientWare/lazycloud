from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.reactive import reactive
from textual.widgets import DataTable, Static

from lazycloud_cli.ui.textual.components import Container
from lazycloud_cli.ui.textual.components.section import SectionContainer
from lazycloud_cli.ui.textual.theme import Icons
from shared.models.billing import STORAGE_CLASS_S3
from shared.responses.usage import (
    ServiceCostBreakdown,
    VolumeCostBreakdown,
    WorkspaceUsageWithDeploymentsResponse,
)


class UsageMetricsTable(DataTable):
    """Table displaying usage metrics with costs"""

    def __init__(self, **kwargs):
        super().__init__(show_header=True, id="usage-metrics-table", **kwargs)
        self.can_focus = False
        self.show_cursor = False
        self.zebra_stripes = True
        self.add_columns("Metric", "Usage (core-hrs / GB-hrs)", "Cost ($)")

    def update_metrics(
        self,
        cpu_hours,
        memory_hours,
        s3_hours,
        efs_hours,
        cpu_cost=None,
        memory_cost=None,
        s3_cost=None,
        efs_cost=None,
    ):
        """Update the table with usage metrics and costs"""
        self.clear()
        cpu_cost_str = f"{cpu_cost:.2f}" if cpu_cost is not None else "-"
        memory_cost_str = f"{memory_cost:.2f}" if memory_cost is not None else "-"
        s3_cost_str = f"{s3_cost:.2f}" if s3_cost is not None else "-"
        efs_cost_str = f"{efs_cost:.2f}" if efs_cost is not None else "-"

        self.add_row("CPU", f"{cpu_hours:.2f}", cpu_cost_str, key="cpu")
        self.add_row("Memory", f"{memory_hours:.2f}", memory_cost_str, key="memory")
        self.add_row("Standard Storage", f"{s3_hours:.2f}", s3_cost_str, key="s3")
        self.add_row("Performance Storage", f"{efs_hours:.2f}", efs_cost_str, key="efs")


class ServicesCostTable(DataTable):
    """Table displaying service cost breakdown with usage"""

    def __init__(self, **kwargs):
        super().__init__(show_header=True, id="services-cost-table", **kwargs)
        self.can_focus = False
        self.show_cursor = False
        self.zebra_stripes = True
        self.add_columns("Service", "CPU (core-hrs)", "Memory (GB-hrs)", "Cost ($)")

    def update_services(self, services: list[ServiceCostBreakdown]):
        """Update the table with service cost and usage data"""
        self.clear()
        for idx, service in enumerate(services):
            # Get usage data from service breakdown (if available)
            cpu_usage_str = "-"
            memory_usage_str = "-"
            if service.cpu_core_hours is not None:
                cpu_usage_str = f"{service.cpu_core_hours:.2f}"
            if service.memory_gb_hours is not None:
                memory_usage_str = f"{service.memory_gb_hours:.2f}"

            cost_str = f"{service.total_compute_cost:.4f}"
            self.add_row(
                service.service_name,
                cpu_usage_str,
                memory_usage_str,
                cost_str,
                key=str(idx),
            )


class VolumesCostTable(DataTable):
    """Table displaying volume cost breakdown"""

    def __init__(self, **kwargs):
        super().__init__(show_header=True, id="volumes-cost-table", **kwargs)
        self.can_focus = False
        self.show_cursor = False
        self.zebra_stripes = True
        self.add_columns("Volume", "Type", "Cost ($)")

    def update_volumes(self, volumes: list[VolumeCostBreakdown]):
        """Update the table with volume cost data"""
        self.clear()
        for idx, volume in enumerate(volumes):
            storage_type = (
                "Standard"
                if volume.storage_class == STORAGE_CLASS_S3
                else "Performance"
            )
            self.add_row(
                volume.volume_name,
                storage_type,
                f"{volume.storage_cost:.4f}",
                key=str(idx),
            )


class DeploymentBreakdownSection(Container):
    """Section showing deployment breakdown with services and volumes"""

    usage_data: reactive[WorkspaceUsageWithDeploymentsResponse | None] = reactive(None)
    selected_deployment_id: reactive[str | None] = reactive(None)

    def __init__(self):
        super().__init__(id="deployment-breakdown-section")
        self._scroll: VerticalScroll | None = None
        self._usage_table: UsageMetricsTable | None = None
        self._services_table: ServicesCostTable | None = None
        self._volumes_table: VolumesCostTable | None = None

    def compose(self) -> ComposeResult:
        """Compose the breakdown section"""
        self._scroll = VerticalScroll(id="breakdown-scroll")
        yield self._scroll

    def on_mount(self) -> None:
        """Ensure the container fills available space"""
        self.styles.width = "100%"
        self.styles.height = "100%"
        self.border_title = f"{Icons.OVERVIEW} Usage Summary"
        if self._scroll:
            self._scroll.styles.width = "100%"
            self._scroll.styles.height = "100%"

    def watch_usage_data(
        self, usage: WorkspaceUsageWithDeploymentsResponse | None
    ) -> None:
        """Update display when usage data changes"""
        self._update_display()

    def watch_selected_deployment_id(self, deployment_id: str | None) -> None:
        """Update display when deployment selection changes"""
        self._update_display()

    def _update_display(self) -> None:
        """Update the display based on current usage data and deployment selection"""
        if not self._scroll:
            return

        if not self.usage_data:
            self._scroll.remove_children()
            message_section = SectionContainer(f"{Icons.OVERVIEW} Usage Breakdown")
            self._scroll.mount(message_section)
            message_section.mount(Static("Loading usage data..."))
            return

        if not self.selected_deployment_id:
            self._scroll.remove_children()
            message_section = SectionContainer(f"{Icons.OVERVIEW} Usage Breakdown")
            self._scroll.mount(message_section)
            message_section.mount(Static("Select a deployment to view breakdown"))
            return

        # Find the selected deployment
        deployment = next(
            (
                d
                for d in self.usage_data.deployments
                if d.deployment_id == self.selected_deployment_id
            ),
            None,
        )

        if not deployment:
            self._scroll.remove_children()
            message_section = SectionContainer(f"{Icons.OVERVIEW} Usage Breakdown")
            self._scroll.mount(message_section)
            message_section.mount(Static("Deployment not found in usage data"))
            return

        # Clear existing sections and rebuild
        self._scroll.remove_children()

        # Add bold "Deployment Cost" title above the section if costs are available
        if deployment.usage.costs:
            cost_title = Static(
                f"[bold]Deployment Cost: ${deployment.usage.costs.total_cost:.2f}[/bold]",
                markup=True,
                id="deployment-cost-title",
            )
            self._scroll.mount(cost_title)

        # Usage metrics section with "Usage Breakdown" title
        usage_container = SectionContainer(title="Usage Breakdown")
        self._scroll.mount(usage_container)

        self._usage_table = UsageMetricsTable()
        usage_container.mount(self._usage_table)

        # Extract costs if available
        costs = deployment.usage.costs
        self._usage_table.update_metrics(
            cpu_hours=deployment.usage.cpu_core_hours,
            memory_hours=deployment.usage.memory_gb_hours,
            s3_hours=deployment.usage.s3_gb_hours,
            efs_hours=deployment.usage.efs_gb_hours,
            cpu_cost=costs.cpu_cost if costs else None,
            memory_cost=costs.memory_cost if costs else None,
            s3_cost=costs.s3_cost if costs else None,
            efs_cost=costs.efs_cost if costs else None,
        )

        # Service Breakdown section
        if deployment.cost_breakdown and deployment.cost_breakdown.service_breakdown:
            services_section = SectionContainer(f"{Icons.WRENCH} Service Breakdown")
            self._scroll.mount(services_section)
            self._services_table = ServicesCostTable()
            services_section.mount(self._services_table)
            self._services_table.update_services(
                deployment.cost_breakdown.service_breakdown,
            )

        # Volumes section
        if deployment.cost_breakdown and deployment.cost_breakdown.volume_breakdown:
            volumes_section = SectionContainer(f"{Icons.SAVE} Volumes")
            self._scroll.mount(volumes_section)
            self._volumes_table = VolumesCostTable()
            volumes_section.mount(self._volumes_table)
            self._volumes_table.update_volumes(
                deployment.cost_breakdown.volume_breakdown
            )
