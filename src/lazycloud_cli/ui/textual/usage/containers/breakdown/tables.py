from textual.widgets import DataTable

from shared.models.billing import STORAGE_CLASS_S3
from shared.responses.usage import ServiceCostBreakdown, VolumeCostBreakdown


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
