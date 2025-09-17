from datetime import datetime

from rich.console import Console
from rich.table import Table
from rich.text import Text

from lazycloud_cli.ui.components.badges import StatusBadge
from lazycloud_cli.ui.components.card import Card, CardGroup
from lazycloud_cli.ui.theme import theme
from shared.responses.deployments import (
    DeploymentResponse,
    RestartResponse,
    RestartServiceResult,
)


class RestartSummaryCard(Card):
    """Card showing restart operation summary."""

    def __init__(self, deployment_name: str, service: str | None = None):
        """Initialize restart summary card."""
        table = Table(show_header=False, box=None)
        table.add_column("Property", style=theme.primary)
        table.add_column("Value")

        table.add_row("Deployment", deployment_name)

        if service:
            table.add_row("Service", service)
            table.add_row("Action", "Restart Service")
        else:
            table.add_row("Action", "Restart All Services")

        table.add_row("Initiated", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

        super().__init__(
            content=table,
            title="🔄 Restart Operation",
            border_style=theme.border_info,
        )


class RestartResultCard(Card):
    """Card for displaying restart results."""

    def __init__(self, response: RestartResponse, service: str | None = None):
        """Initialize restart result card."""
        if service:
            # Single service restart
            content = self._create_single_service_content(response)
            title = f"🔄 Service: {service}"
            border_style = (
                theme.border_success if response.success else theme.border_error
            )
        else:
            # Multiple services restart
            content = self._create_multiple_services_content(response)
            failed = response.failed or 0
            if failed > 0:
                title = "🔄 Restart Results"
                border_style = theme.border_warning
            else:
                title = "🔄 Restart Results"
                border_style = theme.border_success

        super().__init__(
            content=content,
            title=title,
            border_style=border_style,
        )

    def _create_single_service_content(self, response: RestartResponse) -> Table:
        """Create content for single service restart."""
        table = Table(show_header=False, box=None)
        table.add_column("Property", style=theme.primary)
        table.add_column("Value")

        # Status
        if response.success:
            status_badge = StatusBadge("success", "✓ Restarted successfully")
            table.add_row("Status", status_badge)
            table.add_row(
                "", Text("The service is now restarting", style=theme.text_secondary)
            )

        else:
            status_badge = StatusBadge("error", "✗ Restart failed")
            table.add_row("Status", status_badge)
            # Only show error details if available
            if response.error:
                table.add_row("Error", Text(response.error, style=theme.error))

        return table

    def _create_multiple_services_content(self, response: RestartResponse) -> Table:
        """Create content for multiple services restart."""
        table = Table(show_header=False, box=None)
        table.add_column("Property", style=theme.primary)
        table.add_column("Value")

        # Summary stats
        total = response.total_services or 0
        successful = response.successful or 0
        failed = response.failed or 0

        table.add_row("Total Services", str(total))
        table.add_row("Successful", Text(str(successful), style=theme.success))
        if failed > 0:
            table.add_row("Failed", Text(str(failed), style=theme.error))

        return table


class RestartDetailsCard(Card):
    """Card showing detailed results for each service."""

    def __init__(self, results: list[RestartServiceResult]):
        """Initialize restart details card."""
        table = Table(
            show_header=True, header_style=theme.table_header, box=None, expand=True
        )

        table.add_column("Service", style=theme.primary, width=25)
        table.add_column("Status", justify="center", width=20)
        table.add_column("Details", style=theme.text_secondary, overflow="fold")

        for svc_result in results:
            service_name = svc_result.service

            if svc_result.success:
                status_badge = StatusBadge("success", "✓ Restarted")
                message = "Successfully triggered restart"

            else:
                status_badge = StatusBadge("error", "✗ Failed")
                message = "Failed to restart"
                # Only append error details if they're not too technical
                if svc_result.error and not any(
                    kube_term in svc_result.error.lower()
                    for kube_term in ["deployment", "statefulset", "kubectl"]
                ):
                    message = f"Failed: {svc_result.error}"

            table.add_row(service_name, status_badge, message)

        # Determine overall status for border
        failed_count = sum(1 for r in results if not r.success)
        if failed_count > 0:
            border_style = theme.border_warning
            title = f"🔄 Service Details ({failed_count} failed)"

        else:
            border_style = theme.border_success
            title = "🔄 Service Details"

        super().__init__(
            content=table,
            title=title,
            border_style=border_style,
        )


class RestartProgressCard(Card):
    """Card showing restart operation in progress."""

    def __init__(self, deployment_name: str, service: str | None = None):
        """Initialize restart progress card."""
        table = Table(show_header=False, box=None)
        table.add_column("Status", style=theme.warning)

        if service:
            table.add_row(f"⏳ Restarting service '{service}'...")
        else:
            table.add_row("⏳ Restarting all services...")

        table.add_row(Text("This may take a few moments", style=theme.text_secondary))

        super().__init__(
            content=table,
            title=f"Deployment: {deployment_name}",
            border_style=theme.border_warning,
        )


class RestartView:
    """Main view orchestrator for restart operations."""

    def __init__(self):
        """Initialize the restart view."""
        self.console = Console()

    def show_restart_initiation(
        self, deployment: DeploymentResponse, service: str | None = None
    ):
        """Show restart initiation information."""
        summary_card = RestartSummaryCard(
            deployment_name=deployment.name, service=service
        )
        self.console.print(summary_card)

    def show_restart_progress(self, deployment_name: str, service: str | None = None):
        """Show restart in progress."""
        progress_card = RestartProgressCard(deployment_name, service)
        self.console.print(progress_card)

    def show_restart_result(self, restart_response: RestartResponse):
        """Display the result of a restart operation."""
        if restart_response.success:
            # Create result summary card
            result_card = RestartResultCard(
                restart_response, restart_response.service_name
            )

            # For multiple services with detailed results, show details card
            if restart_response.results:
                # Create card group with both summary and details
                details_card = RestartDetailsCard(restart_response.results)
                card_group = CardGroup(cards=[result_card, details_card], spacing=1)
                self.console.print(card_group)
            else:
                self.console.print(result_card)
        else:
            # Error case
            self.show_error(restart_response.message)

    def show_error(self, message: str):
        """Show error message."""
        error_card = Card(
            content=Text(f"{message}", style=theme.error),
            title="❌ Restart Failed",
            border_style=theme.border_error,
        )
        self.console.print(error_card)
