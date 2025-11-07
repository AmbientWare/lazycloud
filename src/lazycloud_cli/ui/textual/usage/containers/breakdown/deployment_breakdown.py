import asyncio

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.reactive import reactive
from textual.widgets import Static

from lazycloud_cli.api.usage import UsageAPI
from lazycloud_cli.ui.textual.components import Container
from lazycloud_cli.ui.textual.components.section import SectionContainer
from lazycloud_cli.ui.textual.theme import Icons
from lazycloud_cli.ui.textual.usage.containers.breakdown.tables import (
    ServicesCostTable,
    UsageMetricsTable,
    VolumesCostTable,
)
from shared.responses.usage import (
    WorkspaceCostBreakdownResponse,
    WorkspaceUsageWithDeploymentsResponse,
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
        self._usage_api = UsageAPI()
        self._loaded_breakdowns: dict[str, WorkspaceCostBreakdownResponse] = {}
        self._loading_breakdowns: set[str] = set()
        self._breakdown_timer = None
        self._fetch_worker = None

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
        # Cancel any pending fetches when usage data changes (new date range)
        if self._fetch_worker and not self._fetch_worker.is_finished:
            self._fetch_worker.cancel()
            self._fetch_worker = None

        # Clear timer if pending
        if self._breakdown_timer:
            self._breakdown_timer = None

        # Clear loaded breakdowns when usage data changes (new date range)
        self._loaded_breakdowns.clear()
        self._loading_breakdowns.clear()
        self._update_display()

    def watch_selected_deployment_id(self, deployment_id: str | None) -> None:
        """Update display when deployment selection changes with debouncing"""
        # Cancel any in-flight fetch when selection changes
        if self._fetch_worker and not self._fetch_worker.is_finished:
            self._fetch_worker.cancel()
            self._fetch_worker = None

        # Clear loading state for previous deployment
        self._loading_breakdowns.clear()

        if not deployment_id:
            self._update_display()
            return

        # Check if we already have breakdown for this deployment
        has_breakdown = deployment_id in self._loaded_breakdowns

        # Only show display immediately if we have complete data, otherwise wait
        if has_breakdown:
            self._update_display()

        # Debounce the breakdown fetch
        self._breakdown_timer = self.handle_debounce(
            self._breakdown_timer,
            lambda: self._fetch_breakdown_after_debounce(deployment_id),
        )

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

        # Get breakdown from loaded breakdowns
        breakdown = None
        if self.selected_deployment_id in self._loaded_breakdowns:
            breakdown = self._loaded_breakdowns[self.selected_deployment_id]

        # Only show display if breakdown is available (not while loading)
        # This prevents jumpy UI - we show everything at once when complete
        if not breakdown:
            self._scroll.remove_children()
            return

        # Clear existing sections and rebuild - only when breakdown is ready
        self._scroll.remove_children()

        # Add bold "Deployment Cost" title above the section if costs are available
        if deployment.usage.costs:
            cost_title = Static(
                f"[bold]Deployment Cost: ${deployment.usage.costs.total_cost:.2f}[/bold]",
                markup=True,
            )
            self._scroll.mount(cost_title)
            # Add spacer for visual separation
            spacer = Static("")
            spacer.styles.height = 1
            self._scroll.mount(spacer)

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
        if breakdown.service_breakdown:
            services_section = SectionContainer(f"{Icons.WRENCH} Service Breakdown")
            self._scroll.mount(services_section)
            self._services_table = ServicesCostTable()
            services_section.mount(self._services_table)
            self._services_table.update_services(breakdown.service_breakdown)

        # Volumes section
        if breakdown.volume_breakdown:
            volumes_section = SectionContainer(f"{Icons.SAVE} Volumes")
            self._scroll.mount(volumes_section)
            self._volumes_table = VolumesCostTable()
            volumes_section.mount(self._volumes_table)
            self._volumes_table.update_volumes(breakdown.volume_breakdown)

    def _fetch_breakdown_after_debounce(self, deployment_id: str) -> None:
        """Fetch breakdown after debounce delay"""
        self._breakdown_timer = None
        # Only fetch if this deployment is still selected
        if deployment_id and self.selected_deployment_id == deployment_id:
            self._fetch_worker = self.run_worker(
                self._fetch_breakdown_async(deployment_id), exclusive=False
            )

    async def _fetch_breakdown_async(self, deployment_id: str) -> None:
        """Fetch deployment cost breakdown asynchronously"""
        if not self.usage_data:
            return

        # Skip if already loaded or is loading
        if (
            deployment_id in self._loaded_breakdowns
            or deployment_id in self._loading_breakdowns
        ):
            return

        # Mark as loading
        self._loading_breakdowns.add(deployment_id)

        try:
            # Check if still selected before fetching (may have changed during debounce)
            if self.selected_deployment_id != deployment_id:
                return

            # Get date range from usage_data
            start_date = self.usage_data.period.start
            end_date = self.usage_data.period.end

            # Fetch breakdown
            breakdown = await asyncio.to_thread(
                self._usage_api.get_deployment_cost_breakdown,
                deployment_id,
                start_date=start_date,
                end_date=end_date,
            )

            # Only store and update if this deployment is still selected
            if self.selected_deployment_id == deployment_id:
                self._loaded_breakdowns[deployment_id] = breakdown

        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.log.error(
                f"Failed to fetch breakdown for deployment {deployment_id}: {e}"
            )
        finally:
            self._loading_breakdowns.discard(deployment_id)
            if self.selected_deployment_id == deployment_id:
                self.call_after_refresh(self._update_display)
