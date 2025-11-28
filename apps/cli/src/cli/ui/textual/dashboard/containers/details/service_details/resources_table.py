from models.k8s import Resources
from textual.widgets import DataTable

from cli.utils.utils import format_cpu, format_memory


class ResourcesTable(DataTable):
    """Non-interactive table displaying resource configuration."""

    def __init__(self, **kwargs):
        super().__init__(show_header=True, id="service-resources-table", **kwargs)
        self.can_focus = False
        self.show_cursor = False
        self.zebra_stripes = True
        self.add_columns("Resource", "Requests", "Limits")

    def update_resources(self, resources: Resources) -> None:
        """Update the table with resource data."""
        self.clear()

        limits = resources.limits
        requests = resources.requests

        # CPU row - format as decimal cores
        cpu_request = (
            f"{format_cpu(requests.cpu)} cores" if requests and requests.cpu else "-"
        )
        cpu_limit = f"{format_cpu(limits.cpu)} cores" if limits and limits.cpu else "-"
        self.add_row("CPU", cpu_request, cpu_limit, key="cpu")

        # Memory row - format with human-readable units
        mem_request = (
            format_memory(requests.memory) if requests and requests.memory else "-"
        )
        mem_limit = format_memory(limits.memory) if limits and limits.memory else "-"
        self.add_row("Memory", mem_request, mem_limit, key="memory")
