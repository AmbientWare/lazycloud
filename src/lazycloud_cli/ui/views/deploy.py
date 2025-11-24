from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.prompt import Prompt
from rich.rule import Rule
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
from lazycloud_cli.ui.views.helpers.env_helpers import (
    ImportMethod,
    create_env_vars_detected_card,
    filter_secrets_with_values,
    find_env_file,
    get_remaining_vars,
    handle_missing_vars_prompt,
    load_from_env_file,
    load_from_shell_env,
    mark_secrets_as_empty,
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

        # Create card with table content based on status
        status_lower = status.lower()
        if status_lower in ["deployed", "completed", "success"]:
            border_style = Colors.Ansi.success
            title = "✅ Deployment Successful"
        elif status_lower in ["failed", "error"]:
            border_style = Colors.Ansi.error
            title = "🛑 Deployment Failed"
        elif status_lower in ["deploying", "pending", "in_progress", "starting"]:
            border_style = Colors.Ansi.info
            title = "🚀 Deployment In Progress"
        else:
            border_style = Colors.Ansi.warning
            title = "⚠️ Deployment Status"

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

    def show_collection_summary(self, collected: int, total: int) -> None:
        """Show summary of environment variable collection."""
        if collected == total:
            message = f"Collected all {collected} environment variable(s)"
        else:
            skipped = total - collected
            message = f"Collected {collected}/{total} variable(s) ({skipped} skipped)"

        self.show_info(message, title="Collection Summary")

    def show_warning(self, message: str) -> None:
        """Show warning message."""
        self.console.print(WarningCard(title="Warning", message=message))

    def show_success(self, message: str, title: str = "Success") -> None:
        """Show success message."""
        self.console.print(SuccessCard(title=title, message=message))

    def show_cancelled(self) -> None:
        """Show deployment cancelled message."""
        card = Card(
            content=Text(
                "Deployment cancelled. No changes were made.",
                style=Colors.Ansi.warning,
            ),
            title="🚫 Cancelled",
            border_style=Colors.Ansi.warning,
        )
        self.console.print(card)

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
            # Show header before diff
            self.console.print()
            self.console.print(Rule("DEPLOYMENT CHANGES", style=Colors.Ansi.primary))
            self.console.print()

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

    def confirm_deployment(
        self,
        deployment_name: str,
        default: bool = True,
        env_source_info: str | None = None,
    ) -> bool:
        """Show deployment confirmation prompt."""
        details = [
            "The deployment will be created/updated",
            "Services will be started in the cluster",
            "Resources will be allocated as configured",
        ]

        if env_source_info:
            details.append(f"Environment variables from: {env_source_info}")

        dialog = SimpleConfirmationDialog(
            action=f"deploy '{deployment_name}'",
            details=details,
            title="🚀 Confirm Deployment",
        )
        dialog.default = default
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

    def collect_secrets(
        self,
        env_vars: SecretCollection,
        project_dir: Path | None = None,
        env_source: str | None = None,
        skip_prompts: bool = False,
    ) -> SecretCollection:
        """Collect secret values for environment variables from user."""
        if not env_vars.added:
            return env_vars

        secrets_dict = {secret.key: secret for secret in env_vars.added}

        # Check for .env file
        env_file = None
        if project_dir:
            env_file = find_env_file(project_dir)

        # Determine source: use provided or ask user
        import_method: ImportMethod
        env_file_path: Path | None = None

        if env_source:
            # Check if it's 'shell', 'none', or a file path
            if env_source.lower() == ImportMethod.SHELL:
                import_method = ImportMethod.SHELL
            elif env_source.lower() == ImportMethod.NONE:
                import_method = ImportMethod.NONE
            else:
                # Treat as file path
                import_method = ImportMethod.FILE
                env_file_path = Path(env_source)
                if not env_file_path.is_absolute():
                    env_file_path = Path.cwd() / env_file_path
        else:
            # Ask user to choose source
            card = create_env_vars_detected_card(len(secrets_dict))
            self.console.print(card)

            choice = Prompt.ask(
                Text("Import from", style=Colors.Ansi.text_muted),
                choices=["file", "shell", "none"],
                default="file",
            )
            import_method = ImportMethod(choice)

        loaded_keys = set()

        if import_method == ImportMethod.SHELL:
            self.console.print()
            # Import from shell environment variables
            loaded_keys = load_from_shell_env(secrets_dict)

            if loaded_keys:
                self.show_info(
                    f"Loaded {len(loaded_keys)} of {len(secrets_dict)} variables from shell environment",
                    title="Import Successful",
                )
            else:
                self.show_warning("No matching variables found in shell environment")

            # Check for remaining missing variables after import
            remaining_vars = get_remaining_vars(secrets_dict, loaded_keys)

            if remaining_vars:
                if not handle_missing_vars_prompt(
                    self.console, remaining_vars, "shell environment", skip_prompts
                ):
                    raise typer.Exit(0)

                mark_secrets_as_empty(secrets_dict, set(remaining_vars.keys()))

        elif import_method == ImportMethod.FILE:
            # User chose to import from file
            if env_file_path:
                # File path provided via --env-source flag
                file_path = env_file_path
            else:
                # Prompt for file path with default if .env exists
                if env_file:
                    # Show relative path from current directory
                    try:
                        relative_path = env_file.relative_to(Path.cwd())
                        default_path = str(relative_path)
                    except ValueError:
                        # If can't make relative (different drive on Windows), use name only
                        default_path = env_file.name

                    file_path_str = Prompt.ask(
                        Text("Path to .env file", style=Colors.Ansi.text_muted),
                        default=default_path,
                    )
                else:
                    file_path_str = Prompt.ask(
                        Text("Path to .env file", style=Colors.Ansi.text_muted),
                        default=".env",
                    )

                # add space for formatting
                self.console.print()

                file_path = Path(file_path_str)
                # Resolve relative to current directory
                if not file_path.is_absolute():
                    file_path = Path.cwd() / file_path

            # Parse the file and load values
            if file_path.exists():
                loaded_keys = load_from_env_file(secrets_dict, file_path)

                if loaded_keys:
                    self.show_info(
                        f"Loaded {len(loaded_keys)} of {len(secrets_dict)} variables from {file_path.name}",
                        title="Import Successful",
                    )
                else:
                    self.show_warning(
                        f"No matching variables found in {file_path.name}"
                    )
            else:
                self.show_error(f"File not found: {file_path}")

            # Check for remaining missing variables after import
            remaining_vars = get_remaining_vars(secrets_dict, loaded_keys)

            if remaining_vars:
                if not handle_missing_vars_prompt(
                    self.console, remaining_vars, "file", skip_prompts
                ):
                    raise typer.Exit(0)

                mark_secrets_as_empty(secrets_dict, set(remaining_vars.keys()))

        else:
            # User chose 'none' or no valid import method
            self.console.print()

            # Show truncated list for large number of vars
            var_list = ", ".join(list(secrets_dict.keys())[:5])
            if len(secrets_dict) > 5:
                var_list += f" and {len(secrets_dict) - 5} more..."

            remaining_vars_display = {var_list: ""}
            if not handle_missing_vars_prompt(
                self.console, remaining_vars_display, "user input", skip_prompts
            ):
                raise typer.Exit(0)

            mark_secrets_as_empty(secrets_dict, set(secrets_dict.keys()))

        # Filter out empty values - only keep secrets that have actual values
        env_vars.added = filter_secrets_with_values(secrets_dict)

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
        table.add_column("Image", style=Colors.Ansi.text_muted, overflow="ellipsis")
        table.add_column("Status", style=Colors.Ansi.text_muted, width=15)
        table.add_column("Details", style=Colors.Ansi.text_muted)

        for i, service in enumerate(self.services):
            status = self.statuses[i]
            message = self.messages[i]

            # Status display with appropriate style
            status_styles = {
                TaskStatus.PENDING: Colors.Ansi.text_muted,
                TaskStatus.COMPLETED: Colors.Ansi.success,
                TaskStatus.ERROR: Colors.Ansi.error,
            }

            status_display = Text(
                status.replace("_", " ").title(),
                style=status_styles.get(status, Colors.Ansi.text_muted),
            )

            table.add_row(
                service["service_name"], service["image_name"], status_display, message
            )

        # Add timing info
        duration = int((datetime.now() - self.start_time).total_seconds())
        if duration > 0:
            table.add_row(
                "", "", "", Text(f"Time: {duration}s", style=Colors.Ansi.text_muted)
            )

        # Determine border style
        if any(s == TaskStatus.ERROR for s in self.statuses):
            border_style = Colors.Ansi.error
        elif all(s == TaskStatus.COMPLETED for s in self.statuses):
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
