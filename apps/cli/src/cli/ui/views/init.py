from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from cli.ui.colors import Colors
from cli.ui.components.card import Card
from cli.ui.components.confirmation import SimpleConfirmationDialog
from cli.ui.components.info_cards import ErrorCard
from cli.ui.components.section import Section


class ComposeFileCard(Card):
    """Card for displaying found compose files."""

    def __init__(self, compose_files: list[str]):
        """Initialize compose file card."""
        table = Table(show_header=False, box=None)
        table.add_column("Index", style=Colors.Ansi.primary, width=6)
        table.add_column("File", style=Colors.Ansi.text)

        for i, file in enumerate(compose_files, 1):
            table.add_row(f"{i}.", file)

        super().__init__(
            content=table,
            title="Found Compose Files",
            subtitle=f"{len(compose_files)} files",
            border_style=Colors.Ansi.info,
        )


class InitConfigCard(Card):
    """Card for displaying initialization configuration."""

    def __init__(
        self,
        deployment_name: str,
        compose_file: str,
        is_sync: bool = False,
    ):
        """Initialize config card."""
        table = Table(show_header=False, box=None)
        table.add_column("Property", style=Colors.Ansi.primary, width=18)
        table.add_column("Value", style=Colors.Ansi.text)

        table.add_row("Deployment Name", deployment_name)
        table.add_row("Compose File", compose_file)
        if is_sync:
            table.add_row("", "[dim]Syncing existing deployment[/dim]")

        super().__init__(
            content=table,
            title="Configuration",
            border_style=Colors.Ansi.primary,
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
        result = Prompt.ask(
            "Enter deployment name",
            default=suggested_name,
            show_default=True,
        )
        self.console.print()
        return result

    def show_validation_error(self, error_message: str):
        """Show deployment name validation error."""
        card = ErrorCard(message=error_message, title="Validation Error")
        self.console.print(card)

    def show_no_compose_files(self) -> str:
        """Show warning when no compose files found and prompt for filename."""
        card = Card(
            content=Text(
                "No docker-compose files found in the current directory.\n"
                "You can specify a compose file that will be created later.",
                style=Colors.Ansi.warning,
            ),
            title="No Compose Files Found",
            border_style=Colors.Ansi.warning,
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
                style=Colors.Ansi.error,
            ),
            title="File Not Found",
            border_style=Colors.Ansi.error,
        )
        self.console.print(card)

    def show_configuration_summary(
        self,
        deployment_name: str,
        compose_file: str,
        is_sync: bool = False,
    ):
        """Show the initialization configuration summary."""
        section = Section(
            title="Initialization Summary",
            content=InitConfigCard(deployment_name, compose_file, is_sync=is_sync),
            icon="✅",
        )
        self.console.print(section)

    def show_success(
        self,
        deployment_name: str,
        compose_file: str,
    ):
        """Show successful initialization message."""
        # Create success content
        content = Text()
        content.append(
            "✅ Successfully created .lazycloud file\n\n", style=Colors.Ansi.success
        )
        content.append("Configuration:\n", style=f"bold {Colors.Ansi.primary}")
        content.append(f"  • Deployment: {deployment_name}\n", style=Colors.Ansi.text)
        content.append(f"  • Compose file: {compose_file}\n", style=Colors.Ansi.text)

        content.append("\nNext steps:\n", style=f"bold {Colors.Ansi.primary}")
        content.append("  • Run ", style=Colors.Ansi.text_muted)
        content.append("lazycloud deploy", style=f"bold {Colors.Ansi.primary}")
        content.append(" to deploy your application\n", style=Colors.Ansi.text_muted)
        content.append("  • Run ", style=Colors.Ansi.text_muted)
        content.append("lazycloud dashboard", style=f"bold {Colors.Ansi.primary}")
        content.append(" to view your deployment\n", style=Colors.Ansi.text_muted)

        card = Card(
            content=content,
            title="Initialization Complete",
            border_style=Colors.Ansi.success,
        )
        self.console.print(card)

    def show_error(self, message: str):
        """Show general error message."""
        card = ErrorCard(message=message, title="Error")
        self.console.print(card)

    def show_info(self, message: str):
        """Show informational message."""
        self.console.print(Text(f"ℹ️  {message}", style=Colors.Ansi.info))

    def show_single_compose_file(self, compose_file: str):
        """Show info when a single compose file is found."""
        card = Card(
            content=Text(f"Found compose file: {compose_file}", style=Colors.Ansi.text),
            title="Auto-detected",
            border_style=Colors.Ansi.info,
        )
        self.console.print(card)

    def show_sync_success(
        self,
        deployment_name: str,
        compose_file: str,
        deployment_info=None,
    ):
        """Show successful sync message for existing deployment."""
        # Create success content
        content = Text()
        content.append(
            "✅ Successfully synced deployment locally\n\n", style=Colors.Ansi.success
        )
        content.append(
            "This directory is now linked to the existing deployment.\n",
            style=Colors.Ansi.text_muted,
        )
        content.append("\nNext steps:\n", style=f"bold {Colors.Ansi.primary}")
        content.append("  • Run ", style=Colors.Ansi.text_muted)
        content.append("lazycloud deploy", style=f"bold {Colors.Ansi.primary}")
        content.append(" to update the deployment\n", style=Colors.Ansi.text_muted)
        content.append("  • Run ", style=Colors.Ansi.text_muted)
        content.append("lazycloud dashboard", style=f"bold {Colors.Ansi.primary}")
        content.append(" to view deployment details\n", style=Colors.Ansi.text_muted)

        card = Card(
            content=content,
            title="Sync Complete",
            border_style=Colors.Ansi.success,
        )
        self.console.print(card)
