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
        self._column_keys: list = []
        self._column_keys = self.add_columns("Resource", "Requests", "Limits")
        self._initialized = False

    def update_resources(self, resources: Resources) -> None:
        """Update the table with resource data using delta updates to prevent flicker."""
        limits = resources.limits
        requests = resources.requests

        # CPU row - format as decimal cores
        cpu_request = (
            f"{format_cpu(requests.cpu)} cores" if requests and requests.cpu else "-"
        )
        cpu_limit = f"{format_cpu(limits.cpu)} cores" if limits and limits.cpu else "-"
        cpu_values = ("CPU", cpu_request, cpu_limit)

        # Memory row - format with human-readable units
        mem_request = (
            format_memory(requests.memory) if requests and requests.memory else "-"
        )
        mem_limit = format_memory(limits.memory) if limits and limits.memory else "-"
        mem_values = ("Memory", mem_request, mem_limit)

        if not self._initialized:
            # First time - add rows
            self.add_row(*cpu_values, key="cpu")
            self.add_row(*mem_values, key="memory")
            self._initialized = True
        else:
            # Update existing rows - only update cells that changed
            for row_key, new_values in [("cpu", cpu_values), ("memory", mem_values)]:
                for col_key, new_value in zip(self._column_keys, new_values):
                    try:
                        current_value = self.get_cell(row_key, col_key)
                        if current_value != new_value:
                            self.update_cell(row_key, col_key, new_value)
                    except Exception:
                        pass
