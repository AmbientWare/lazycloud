from dataclasses import dataclass
from typing import Any

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.prompt import Confirm
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from cli.ui.components.badges import DeploymentStatusBadge


@dataclass
class DeploymentInfo:
    """Data class for deployment information."""

    id: str
    name: str
    state: str
    created_at: str | None = None
    updated_at: str | None = None
    services: list[dict[str, Any]] | None = None
    errors: list[str] | None = None
    warnings: list[str] | None = None


class DeploymentInfoPanel:
    """A panel component for displaying detailed deployment information."""

    def __init__(self, deployment: DeploymentInfo | dict[str, Any]):
        """Initialize the panel with deployment data.

        Args:
            deployment: DeploymentInfo object or dictionary with deployment data
        """
        if isinstance(deployment, dict):
            self.deployment = DeploymentInfo(**deployment)
        else:
            self.deployment = deployment

    def render(self, show_services: bool = True, compact: bool = False) -> Panel:
        """Render the deployment info panel.

        Args:
            show_services: Whether to show the services table
            compact: Whether to use compact layout

        Returns:
            Rich Panel with deployment information
        """
        content = []

        # Status badge
        status_badge = DeploymentStatusBadge(self.deployment.state)

        # Basic info table
        info_table = Table(show_header=False, box=None)
        info_table.add_column("Label", style="dim")
        info_table.add_column("Value")

        info_table.add_row("Status:", status_badge.render())
        info_table.add_row("ID:", Text(self.deployment.id, style="cyan"))

        if self.deployment.created_at:
            info_table.add_row(
                "Created:", Text(self.deployment.created_at, style="dim")
            )

        if self.deployment.updated_at:
            info_table.add_row(
                "Updated:", Text(self.deployment.updated_at, style="dim")
            )

        content.append(info_table)

        # Errors and warnings
        if self.deployment.errors:
            content.append(Text())  # Spacing
            error_text = Text("Errors:", style="bold red")
            content.append(error_text)
            for error in self.deployment.errors:
                content.append(Text(f"  • {error}", style="red"))

        if self.deployment.warnings:
            content.append(Text())  # Spacing
            warning_text = Text("Warnings:", style="bold yellow")
            content.append(warning_text)
            for warning in self.deployment.warnings:
                content.append(Text(f"  • {warning}", style="yellow"))

        # Services table
        if show_services and self.deployment.services:
            content.append(Text())  # Spacing
            services_table = self._create_services_table(compact)
            content.append(services_table)

        # Combine all content
        if compact and len(content) == 1:
            panel_content = content[0]
        else:
            panel_content = Group(*content)

        return Panel(
            panel_content,
            title=f"[bold]{self.deployment.name}[/bold]",
            box=box.ROUNDED,
            expand=False,
        )

    def _create_services_table(self, compact: bool = False) -> Table:
        """Create a table of services.

        Args:
            compact: Whether to use compact layout

        Returns:
            Rich Table with service information
        """
        table = Table(
            title="Services",
            box=box.SIMPLE if compact else box.ROUNDED,
            show_header=True,
            header_style="bold",
        )

        table.add_column("Name", style="cyan")
        table.add_column("Type", style="blue")
        table.add_column("Port", style="green")

        if not compact:
            table.add_column("Status", style="yellow")

        for service in self.deployment.services or []:
            row = [
                service.get("name", ""),
                service.get("type", "ClusterIP"),
                str(service.get("port", "")),
            ]
            if not compact:
                row.append(service.get("status", "Unknown"))
            table.add_row(*row)

        return table

    def __rich__(self) -> Panel:
        """Rich protocol support for direct console printing."""
        return self.render()


class ConfirmationPanel:
    """A panel component for displaying confirmation dialogs."""

    def __init__(
        self,
        message: str,
        title: str = "Confirm Action",
        danger: bool = False,
        details: str | list[str] | None = None,
    ):
        """Initialize the confirmation panel.

        Args:
            message: The main confirmation message
            title: Panel title
            danger: Whether this is a dangerous operation (affects styling)
            details: Additional details to show (string or list of strings)
        """
        self.message = message
        self.title = title
        self.danger = danger
        self.details = (
            details if isinstance(details, list) else [details] if details else []
        )

    def render(self) -> Panel:
        """Render the confirmation panel.

        Returns:
            Rich Panel with confirmation message
        """
        content = []

        # Main message
        message_style = "bold red" if self.danger else "bold yellow"
        content.append(Text(self.message, style=message_style))

        # Details
        if self.details:
            content.append(Text())  # Spacing
            for detail in self.details:
                content.append(Text(f"• {detail}", style="dim"))

        # Warning for dangerous operations
        if self.danger:
            content.append(Text())  # Spacing
            content.append(Text("This action cannot be undone!", style="bold red"))

        panel_content = Group(*content)

        border_style = "red" if self.danger else "yellow"
        return Panel(
            panel_content,
            title=f"[bold]{self.title}[/bold]",
            box=box.HEAVY if self.danger else box.ROUNDED,
            border_style=border_style,
            expand=False,
        )

    def confirm(self, console: Console | None = None) -> bool:
        """Show the panel and prompt for confirmation.

        Args:
            console: Rich Console instance (creates new if not provided)

        Returns:
            True if user confirmed, False otherwise
        """
        console = console or Console()
        console.print(self.render())
        console.print()  # Spacing

        prompt = "[red]Are you sure?[/red]" if self.danger else "Continue?"
        return Confirm.ask(prompt, console=console, default=False)

    def __rich__(self) -> Panel:
        """Rich protocol support for direct console printing."""
        return self.render()


class CommandResult:
    """A component for displaying command results consistently."""

    def __init__(
        self,
        success: bool,
        message: str,
        details: str | list[str] | dict[str, Any] | None = None,
        data: Any | None = None,
    ):
        """Initialize the command result.

        Args:
            success: Whether the command was successful
            message: The main result message
            details: Additional details (string, list, or dict)
            data: Optional data to display (e.g., JSON, YAML)
        """
        self.success = success
        self.message = message
        self.details = details
        self.data = data

    def render(self, show_data: bool = True) -> Panel | Text:
        """Render the command result.

        Args:
            show_data: Whether to show the data section

        Returns:
            Rich Panel or Text with the result
        """
        content = []

        # Status icon and message
        if self.success:
            style = "bold green"
            border_style = "green"
        else:
            style = "bold red"
            border_style = "red"

        content.append(Text(self.message, style=style))

        # Details
        if self.details:
            content.append(Text())  # Spacing

            if isinstance(self.details, dict):
                detail_table = Table(show_header=False, box=None)
                detail_table.add_column("Key", style="dim")
                detail_table.add_column("Value")

                for key, value in self.details.items():
                    detail_table.add_row(f"{key}:", str(value))
                content.append(detail_table)

            elif isinstance(self.details, list):
                for detail in self.details:
                    content.append(Text(f"  • {detail}", style="dim"))

            else:
                content.append(Text(str(self.details), style="dim"))

        # Data section
        if show_data and self.data:
            content.append(Text())  # Spacing

            if isinstance(self.data, (dict, list)):
                import json

                data_str = json.dumps(self.data, indent=2)
                syntax = Syntax(data_str, "json", theme="monokai", line_numbers=False)
                content.append(syntax)
            else:
                content.append(Text(str(self.data), style="dim"))

        # Return appropriate component
        if len(content) == 1 and not show_data:
            return content[0]

        panel_content = Group(*content)

        return Panel(
            panel_content,
            box=box.ROUNDED,
            border_style=border_style,
            expand=False,
        )

    def print(self, console: Console | None = None):
        """Print the result to console.

        Args:
            console: Rich Console instance (creates new if not provided)
        """
        console = console or Console()
        console.print(self.render())

    def __rich__(self) -> Panel | Text:
        """Rich protocol support for direct console printing."""
        return self.render()
