from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from lazycloud_cli.ui.components.card import Card
from lazycloud_cli.ui.components.confirmation import SimpleConfirmationDialog
from lazycloud_cli.ui.components.section import Section
from lazycloud_cli.ui.theme import theme


class ComposeFileCard(Card):
    """Card for displaying found compose files."""

    def __init__(self, compose_files: list[str]):
        """Initialize compose file card."""
        table = Table(show_header=False, box=None)
        table.add_column("Index", style=theme.primary, width=6)
        table.add_column("File", style=theme.text_primary)

        for i, file in enumerate(compose_files, 1):
            table.add_row(f"{i}.", file)

        super().__init__(
            content=table,
            title="Found Compose Files",
            subtitle=f"{len(compose_files)} files",
            border_style=theme.border_info,
        )


class InitConfigCard(Card):
    """Card for displaying initialization configuration."""

    def __init__(
        self,
        deployment_name: str,
        compose_file: str,
        environment: str | None = None,
    ):
        """Initialize config card."""
        table = Table(show_header=False, box=None)
        table.add_column("Property", style=theme.primary)
        table.add_column("Value")

        table.add_row("Deployment Name", deployment_name)
        table.add_row("Compose File", compose_file)
        if environment:
            table.add_row("Environment", environment)

        super().__init__(
            content=table,
            title="Configuration",
            border_style=theme.border_primary,
        )


class ValidationErrorCard(Card):
    """Card for displaying validation errors."""

    def __init__(self, error_message: str):
        """Initialize validation error card."""
        content = Text(f"{error_message}", style=theme.error)

        super().__init__(
            content=content,
            title="Validation Error",
            border_style=theme.border_error,
        )


class InitView:
    """Main view orchestrator for the init command."""

    def __init__(self, console: Console):
        """Initialize the init view."""
        self.console = console

    def show_existing_file_warning(self) -> bool:
        """Show warning about existing .lazycloud file and get user confirmation."""
        dialog = SimpleConfirmationDialog(
            action="overwrite the existing configuration",
            details=[
                "A .lazycloud file already exists in this directory",
                "The current configuration will be replaced",
                "Any deployment references will remain valid",
            ],
            title="Existing Configuration Found",
        )
        dialog.default = False  # Override default to be safer
        return dialog.show(self.console)

    def prompt_deployment_name(self, suggested_name: str) -> str:
        """Prompt for deployment name with suggestion."""
        return Prompt.ask(
            "Enter deployment name",
            default=suggested_name,
            show_default=True,
        )

    def show_validation_error(self, error_message: str):
        """Show deployment name validation error."""
        card = ValidationErrorCard(error_message)
        self.console.print(card)

    def show_no_compose_files(self) -> str:
        """Show warning when no compose files found and prompt for filename."""
        card = Card(
            content=Text(
                "No docker-compose files found in the current directory.\n"
                "You can specify a compose file that will be created later.",
                style=theme.warning,
            ),
            title="No Compose Files Found",
            border_style=theme.border_warning,
        )
        self.console.print(card)
        return Prompt.ask(
            "Enter compose file name",
            default="docker-compose.yml",
        )

    def show_compose_files(self, compose_files: list[str]):
        """Display found compose files."""
        section = Section(
            title="Available Compose Files",
            content=ComposeFileCard(compose_files),
        )
        self.console.print(section)

    def prompt_compose_file_selection(self, compose_files: list[str]) -> str:
        """Prompt user to select from multiple compose files."""
        choice = Prompt.ask(
            "Select compose file",
            choices=[str(i) for i in range(1, len(compose_files) + 1)],
            default="1",
        )
        return compose_files[int(choice) - 1]

    def show_compose_file_not_found(self, compose_file: str):
        """Show error when specified compose file doesn't exist."""
        card = Card(
            content=Text(
                f"Compose file '{compose_file}' not found in the current directory.",
                style=theme.error,
            ),
            title="File Not Found",
            border_style=theme.border_error,
        )
        self.console.print(card)

    def show_configuration_summary(
        self,
        deployment_name: str,
        compose_file: str,
        environment: str | None = None,
    ):
        """Show the initialization configuration summary."""
        section = Section(
            title="Initialization Summary",
            content=InitConfigCard(deployment_name, compose_file, environment),
            icon="✅",
        )
        self.console.print(section)

    def show_success(
        self,
        deployment_name: str,
        compose_file: str,
        environment: str | None = None,
    ):
        """Show successful initialization message."""
        # Create success content
        content = Text()
        content.append(
            "✅ Successfully created .lazycloud file\n\n", style=theme.success
        )
        content.append("Configuration:\n", style=f"bold {theme.primary}")
        content.append(f"  • Deployment: {deployment_name}\n", style=theme.text_primary)
        content.append(f"  • Compose file: {compose_file}\n", style=theme.text_primary)
        if environment:
            content.append(
                f"  • Environment: {environment}\n", style=theme.text_primary
            )

        content.append("\nNext steps:\n", style=f"bold {theme.primary}")
        content.append("  • Run ", style=theme.text_secondary)
        content.append("lazycloud deploy", style=f"bold {theme.primary}")
        content.append(" to deploy your application\n", style=theme.text_secondary)
        content.append("  • Run ", style=theme.text_secondary)
        content.append("lazycloud status", style=f"bold {theme.primary}")
        content.append(" to check deployment status\n", style=theme.text_secondary)

        card = Card(
            content=content,
            title="Initialization Complete",
            border_style=theme.border_success,
        )
        self.console.print(card)

    def show_error(self, message: str):
        """Show general error message."""
        card = Card(
            content=Text(f"❌ {message}", style=theme.error),
            title="Error",
            border_style=theme.border_error,
        )
        self.console.print(card)

    def show_info(self, message: str):
        """Show informational message."""
        self.console.print(Text(f"ℹ️  {message}", style=theme.info))

    def show_single_compose_file(self, compose_file: str):
        """Show info when a single compose file is found."""
        card = Card(
            content=Text(
                f"Found compose file: {compose_file}", style=theme.text_primary
            ),
            title="Auto-detected",
            border_style=theme.border_info,
        )
        self.console.print(card)

    def show_sync_success(
        self,
        deployment_name: str,
        compose_file: str,
        environment: str | None = None,
    ):
        """Show successful sync message for existing deployment."""
        # Create success content
        content = Text()
        content.append(
            "✅ Successfully synced deployment locally\n\n", style=theme.success
        )
        content.append("Configuration:\n", style=f"bold {theme.primary}")
        content.append(f"  • Deployment: {deployment_name}\n", style=theme.text_primary)
        content.append(f"  • Compose file: {compose_file}\n", style=theme.text_primary)
        if environment:
            content.append(
                f"  • Environment: {environment}\n", style=theme.text_primary
            )

        content.append(
            "\nThis directory is now linked to the existing deployment.\n",
            style=theme.text_secondary,
        )
        content.append("\nNext steps:\n", style=f"bold {theme.primary}")
        content.append("  • Run ", style=theme.text_secondary)
        content.append("lazycloud status", style=f"bold {theme.primary}")
        content.append(" to check deployment status\n", style=theme.text_secondary)
        content.append("  • Run ", style=theme.text_secondary)
        content.append("lazycloud deploy", style=f"bold {theme.primary}")
        content.append(" to update the deployment\n", style=theme.text_secondary)

        card = Card(
            content=content,
            title="Sync Complete",
            border_style=theme.border_success,
        )
        self.console.print(card)
