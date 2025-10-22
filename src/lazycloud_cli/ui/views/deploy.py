"""Deploy command view components - simplified version."""

from datetime import datetime

from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from lazycloud_cli.ui.colors import Colors
from lazycloud_cli.ui.components import (
    Card,
    CardGroup,
    DeploymentProgress,
    DeploymentStatusBadge,
    ErrorCard,
    InfoCard,
    SimpleConfirmationDialog,
    SuccessCard,
    WarningCard,
)
from lazycloud_cli.ui.views.helpers.diff_renderer import (
    create_diff_cards,
    create_env_var_card,
)
from shared.models.secrets import SecretCollection
from shared.models.statuses import TaskStatus
from shared.responses.deployments import DiffResponse


class DeployView:
    """Main view orchestrator for the deploy command."""

    def __init__(self, console: Console):
        """Initialize the deploy view."""
        self.console = console

    def show_configuration(self, deployment_name: str, compose_file: str) -> None:
        """Show deployment configuration."""
        table = Table(show_header=False, box=None)
        table.add_column("Property", style=Colors.Ansi.primary)
        table.add_column("Value")

        table.add_row("Deployment", deployment_name)
        table.add_row("Compose File", compose_file)

        card = Card(
            content=table,
            title="🛠️  Deployment Configuration",
            border_style=Colors.Ansi.primary,
        )
        self.console.print(card)

    def show_build_info(self, services_to_build: list[dict[str, str]]) -> None:
        """Show services that need to be built."""
        if not services_to_build:
            return

        table = Table(show_header=True, box=None)
        table.add_column("Service", style=Colors.Ansi.primary)
        table.add_column("Image")
        table.add_column("Context")

        for service in services_to_build:
            table.add_row(
                service["service_name"],
                service["image_name"],
                service.get("context", "."),
            )

        card = Card(
            content=table,
            title="🏗️  Services to Build",
            subtitle=f"{len(services_to_build)} services",
            border_style=Colors.Ansi.warning,
        )
        self.console.print(card)

    def show_validation(
        self, errors: list[str] | None = None, warnings: list[str] | None = None
    ) -> None:
        """Show validation results."""
        if errors:
            # Create content for multiple errors
            from rich.console import Group

            error_parts = []
            for error in errors:
                error_parts.append(Text(f"• {error}", style=Colors.Ansi.error))

            card = Card(
                content=Group(*error_parts),
                title="❌ Validation Errors",
                border_style=Colors.Ansi.error,
            )
            self.console.print(card)

        if warnings:
            # Create content for multiple warnings
            from rich.console import Group

            warning_parts = []
            for warning in warnings:
                warning_parts.append(Text(f"• {warning}", style=Colors.Ansi.warning))

            card = Card(
                content=Group(*warning_parts),
                title="⚠️ Validation Warnings",
                border_style=Colors.Ansi.warning,
            )
            self.console.print(card)

        if not errors and not warnings:
            self.console.print(
                SuccessCard(title="Validation Successful", message="No issues found")
            )

    def show_progress(self, deployment_name: str) -> DeploymentProgress:
        """Create and return a deployment progress tracker."""
        progress = DeploymentProgress(deployment_name)

        # Add standard deployment steps
        progress.add_step("Validating configuration")
        progress.add_step("Creating resources")
        progress.add_step("Waiting for services to start")
        progress.add_step("Verifying health")

        return progress

    def show_summary(
        self,
        deployment_name: str,
        status: str,
        duration: int | None = None,
        message: str | None = None,
    ) -> None:
        """Show deployment summary."""
        status_badge = DeploymentStatusBadge(status)

        table = Table(show_header=False, box=None)
        table.add_column("Property", style=Colors.Ansi.primary)
        table.add_column("Value")

        table.add_row("Deployment", deployment_name)
        table.add_row("Status", status_badge)

        if duration:
            minutes = duration // 60
            seconds = duration % 60
            table.add_row("Duration", f"{minutes}m {seconds}s")

        if message:
            table.add_row("Message", Text(message, style=Colors.Ansi.text_muted))

        # Create card with table content
        if status.lower() == "deployed":
            border_style = Colors.Ansi.success
            title = "✅  Deployment Successful"
        elif status.lower() == "failed":
            border_style = Colors.Ansi.error
            title = "❌  Deployment Failed"
        else:
            border_style = Colors.Ansi.warning
            title = "⚠️  Deployment Status"

        card = Card(
            content=table,
            title=title,
            border_style=border_style,
        )
        self.console.print(card)

    def show_error(self, message: str, suggestion: str | None = None) -> None:
        """Show error message."""
        self.console.print(
            ErrorCard(title="Error", message=message, suggestion=suggestion)
        )

    def show_info(self, message: str, title: str | None = None) -> None:
        """Show informational message."""
        self.console.print(InfoCard(title=title, message=message))

    def show_warning(self, message: str) -> None:
        """Show warning message."""
        self.console.print(WarningCard(title="Warning", message=message))

    def show_cancelled(self) -> None:
        """Show deployment cancelled message."""
        self.console.print(
            WarningCard(
                title="Deployment Cancelled", message="Operation cancelled by user"
            )
        )

    def show_diff(
        self, diff_response: DiffResponse, show_warnings: bool = False
    ) -> bool:
        """Show deployment diff and return True if there are changes."""
        # Check for validation errors/warnings first
        if diff_response.errors:
            self.show_validation(errors=diff_response.errors)
            return True

        if show_warnings and diff_response.warnings:
            self.show_validation(warnings=diff_response.warnings)

        # Create diff cards
        cards = []

        if diff_response.diff:
            cards.extend(create_diff_cards(diff_response.diff))

        if diff_response.env_var_changes:
            env_card = create_env_var_card(diff_response.env_var_changes)
            if env_card:
                cards.append(env_card)

        if cards:
            card_group = CardGroup(cards=cards, spacing=1)
            self.console.print(card_group)

        # Check if there are changes
        if not cards:
            return False

        return True

    def show_no_changes(self) -> bool:
        """Show no changes detected message."""
        dialog = Card(
            content=Text(
                "We can still build and deploy anyway", style=Colors.Ansi.text_muted
            ),
            title="No Changes Detected",
            border_style=Colors.Ansi.warning,
        )
        self.console.print(dialog)

    def confirm_deployment(self, deployment_name: str) -> bool:
        """Show deployment confirmation prompt."""
        dialog = SimpleConfirmationDialog(
            action=f"deploy '{deployment_name}'",
            details=[
                "The deployment will be created/updated",
                "Services will be started in the cluster",
                "Resources will be allocated as configured",
            ],
            title="🚀 Confirm Deployment",
        )
        return dialog.show(self.console)

    def confirm_continue(self, message: str) -> bool:
        """Generic confirmation prompt."""
        dialog = SimpleConfirmationDialog(
            action="continue",
            details=[message],
            title="Confirmation Required",
        )
        dialog.default = False  # Be cautious by default
        return dialog.show(self.console)

    def show_build_progress(self, services: list[dict[str, str]]) -> "BuildProgress":
        """Create and return a build progress tracker."""
        return BuildProgress(services)

    def show_build_status(self, services: list[dict[str, str]]) -> "BuildProgress":
        """Alias for show_build_progress for compatibility."""
        return self.show_build_progress(services)

    def show_deployment_creation_progress(
        self, deployment_name: str
    ) -> DeploymentProgress:
        """Create and return a deployment creation progress tracker."""
        progress = DeploymentProgress(deployment_name)
        progress.add_step("Initializing deployment")
        progress.add_step("Creating deployment resources")
        progress.add_step("Finalizing deployment")
        return progress

    def collect_secrets(self, env_vars: SecretCollection) -> SecretCollection:
        """Collect secret values for environment variables from user."""
        if env_vars.added:
            self.console.print(
                InfoCard(
                    title="🔐 New Environment Variables",
                    message=(
                        f"Found {len(env_vars.added)} new environment variables that need values.\n\n"
                        "Please enter the values for each variable.\n"
                        "These will be encrypted and stored securely.\n\n"
                        "Press Enter to use the default value shown in brackets."
                    ),
                )
            )

            # Collect value for each env var
            for key, default_value in env_vars.added.items():
                self.console.print(
                    Text(f"Variable: {key}", style=f"bold {Colors.Ansi.primary}")
                )

                value = Prompt.ask(
                    Text("Value", style=Colors.Ansi.text_muted),
                    default=default_value if default_value else None,
                    show_default=True,
                )

                env_vars.added[key] = value if value else default_value

            self.show_info(
                f"Collected values for {len(env_vars.added)} environment variables",
                title="Secrets Collection Complete",
            )

        return env_vars


class BuildProgress:
    """Live progress tracker for building services."""

    def __init__(self, services: list[dict[str, str]]):
        """Initialize build progress."""
        self.services = services
        self.statuses = ["pending"] * len(services)
        self.messages = [""] * len(services)
        self.start_time = datetime.now()

    def update_service(self, index: int, status: TaskStatus, message: str = "") -> None:
        """Update the status of a service."""
        if 0 <= index < len(self.statuses):
            self.statuses[index] = status
            self.messages[index] = message

    def render(self) -> Card:
        """Render the current build status."""
        table = Table(
            show_header=True,
            header_style=f"bold {Colors.Ansi.primary}",
            box=None,
            expand=True,
        )
        table.add_column("Service", style=Colors.Ansi.primary, width=20)
        table.add_column("Image", style=Colors.Ansi.text_secondary, overflow="ellipsis")
        table.add_column("Status", style=Colors.Ansi.text_primary, width=15)
        table.add_column("Details", style=Colors.Ansi.text_secondary)

        for i, service in enumerate(self.services):
            status = self.statuses[i]
            message = self.messages[i]

            # Status display with appropriate style
            status_styles = {
                TaskStatus.PENDING: Colors.Ansi.text_secondary,
                TaskStatus.COMPLETED: Colors.Ansi.success,
                TaskStatus.ERROR: Colors.Ansi.error,
            }

            status_display = Text(
                status.replace("_", " ").title(),
                style=status_styles.get(status, Colors.Ansi.text_secondary),
            )

            table.add_row(
                service["service_name"], service["image_name"], status_display, message
            )

        # Add timing info
        duration = int((datetime.now() - self.start_time).total_seconds())
        if duration > 0:
            table.add_row(
                "", "", "", Text(f"Time: {duration}s", style=Colors.Ansi.text_secondary)
            )

        # Determine border style
        if any(s == TaskStatus.ERROR for s in self.statuses):
            border_style = Colors.Ansi.error
        elif all(
            s in [TaskStatus.COMPLETED, TaskStatus.PENDING] for s in self.statuses
        ):
            border_style = Colors.Ansi.success
        else:
            border_style = Colors.Ansi.info

        return Card(
            content=table,
            title="🏗️  Build Progress",
            border_style=border_style,
        )

    def __rich__(self):
        """Support Rich rendering."""
        return self.render()
