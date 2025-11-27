from textual.widgets import DataTable

from shared.models.billing import STORAGE_CLASS_EBS
from shared.responses.usage import ServiceCostBreakdown, VolumeCostBreakdown


class UsageMetricsTable(DataTable):
    """Table displaying usage metrics with costs"""

    def __init__(self, **kwargs):
        super().__init__(show_header=True, id="usage-metrics-table", **kwargs)
        self.can_focus = False
        self.show_cursor = False
        self.zebra_stripes = True
        self.add_columns("Metric", "Usage (unit-h)", "Cost ($)")

    def update_metrics(
        self,
        cpu_hours,
        memory_hours,
        standard_hours,
        shared_hours,
        build_minutes,
        public_endpoint_hours,
        cpu_cost=None,
        memory_cost=None,
        standard_cost=None,
        shared_cost=None,
        build_cost=None,
        endpoint_cost=None,
    ):
        """Update the table with usage metrics and costs"""
        self.clear()
        cpu_cost_str = f"{cpu_cost:.2f}" if cpu_cost is not None else "-"
        memory_cost_str = f"{memory_cost:.2f}" if memory_cost is not None else "-"

        # Combined storage
        storage_hours = standard_hours + shared_hours
        storage_cost = None
        if standard_cost is not None and shared_cost is not None:
            storage_cost = standard_cost + shared_cost
        storage_cost_str = f"{storage_cost:.2f}" if storage_cost is not None else "-"

        build_cost_str = f"{build_cost:.2f}" if build_cost is not None else "-"
        endpoint_cost_str = f"{endpoint_cost:.2f}" if endpoint_cost is not None else "-"

        self.add_row("CPU (core)", f"{cpu_hours:.2f}", cpu_cost_str, key="cpu")
        self.add_row(
            "Memory (GB)", f"{memory_hours:.2f}", memory_cost_str, key="memory"
        )
        self.add_row(
            "Storage (GB)", f"{storage_hours:.2f}", storage_cost_str, key="storage"
        )
        self.add_row(
            "Build Minutes", f"{build_minutes:.2f}", build_cost_str, key="build"
        )
        self.add_row(
            "Endpoints (hours)",
            f"{public_endpoint_hours:.2f}",
            endpoint_cost_str,
            key="endpoints",
        )


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
                "Standard" if volume.storage_class == STORAGE_CLASS_EBS else "Shared"
            )
            self.add_row(
                volume.volume_name,
                storage_type,
                f"{volume.storage_cost:.4f}",
                key=str(idx),
            )
