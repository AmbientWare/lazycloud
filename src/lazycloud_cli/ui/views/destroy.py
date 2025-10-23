from datetime import datetime
from typing import Any, Dict

from rich.console import Console
from rich.live import Live
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table
from rich.text import Text

from lazycloud_cli.ui.colors import Colors
from lazycloud_cli.ui.components.card import Card
from lazycloud_cli.ui.components.confirmation import (
    StringValidationConfirmationDialog,
)


class DestroyTargetCard(Card):
    """Card showing the deployment targeted for destruction."""

    def __init__(self, deployment: Dict[str, Any]):
        """Initialize destroy target card."""
        content = Text()
        content.append("You are about to destroy:\n\n", style=Colors.Ansi.warning)
        content.append("  Deployment: ", style=Colors.Ansi.text_muted)
        content.append(f"{deployment['name']}\n", style=f"{Colors.Ansi.primary} bold")

        if deployment.get("services_count"):
            content.append("  Services:   ", style=Colors.Ansi.text_muted)
            content.append(
                f"{deployment['services_count']} services\n", style=Colors.Ansi.warning
            )

        content.append("\n", style=f"{Colors.Ansi.error} bold")
        content.append("This action cannot be undone!", style=Colors.Ansi.error)

        super().__init__(
            content=content,
            title="🚨 Destruction Target",
            border_style=Colors.Ansi.error,
        )


class DestroyConfirmationCard(Card):
    """Card for confirming destruction."""

    def __init__(self, deployment_name: str):
        """Initialize confirmation card."""
        content = Text()
        content.append("This will permanently delete:\n\n", style=Colors.Ansi.warning)
        content.append("  • All deployed services\n", style=Colors.Ansi.text_muted)
        content.append("  • All running containers\n", style=Colors.Ansi.text_muted)
        content.append("  • All configuration data\n", style=Colors.Ansi.text_muted)
        content.append("  • All persistent storage\n", style=Colors.Ansi.text_muted)
        content.append("  • All network configurations\n", style=Colors.Ansi.text_muted)

        super().__init__(
            content=content,
            title="⚠️  Confirm Destruction",
            border_style=Colors.Ansi.warning,
        )


class DestroyView:
    """Main view orchestrator for the destroy command."""

    def __init__(self, console: Console):
        """Initialize the destroy view."""
        self.console = console

    def show_target(self, deployment: Dict[str, Any]):
        """Show the deployment targeted for destruction.

        Args:
            deployment: Deployment information dict
        """
        card = DestroyTargetCard(deployment)
        self.console.print(card)

    def confirm_destruction(self, deployment_name: str, force: bool = False) -> bool:
        """Show confirmation prompt for destruction.

        Args:
            deployment_name: Name of deployment to destroy
            force: Skip confirmation if True

        Returns:
            True if confirmed, False otherwise
        """
        if force:
            return True

        # Use the string validation confirmation dialog
        dialog = StringValidationConfirmationDialog(
            resource_type="deployment",
            resource_name=deployment_name,
            consequences=[
                "All deployed services will be terminated",
                "All running containers will be removed",
                "All configuration data will be deleted",
                "All persistent storage will be destroyed",
                "All network configurations will be removed",
            ],
        )

        return dialog.show(self.console)

    def show_progress(self, deployment_name: str):
        """Show destruction progress with elapsed time.

        Args:
            deployment_name: Name of deployment being destroyed

        Returns:
            Context manager for progress display
        """

        class DestroyRenderable:
            """Custom renderable that updates with elapsed time."""

            def __init__(self, deployment_name):
                self.deployment_name = deployment_name
                self.start_time = datetime.now()
                self.progress = Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    transient=False,
                )
                self.task_id = self.progress.add_task(
                    "Removing all services and resources..."
                )

            def __rich__(self):
                """Rich protocol - called each time the display updates."""
                # Create a table for layout
                table = Table.grid(padding=0)
                table.add_column()

                # Add spinner and message row
                table.add_row(self.progress)

                # Add elapsed time row
                elapsed = int((datetime.now() - self.start_time).total_seconds())
                elapsed_text = Text(
                    f"Elapsed time: {elapsed}s", style=Colors.Ansi.text_muted
                )
                table.add_row(elapsed_text)

                return Card(
                    content=table,
                    title=f"💀 Destroying '{self.deployment_name}'",
                    border_style=Colors.Ansi.warning,
                )

        class ProgressContext:
            def __init__(self, console, name):
                self.console = console
                self.deployment_name = name
                self.live = None
                self.renderable = None

            def __enter__(self):
                self.renderable = DestroyRenderable(self.deployment_name)
                self.live = Live(
                    self.renderable, console=self.console, refresh_per_second=4
                )
                self.live.start()
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                if self.live:
                    self.live.stop()
                if self.renderable:
                    self.renderable.progress.stop()

        return ProgressContext(self.console, deployment_name)

    def show_success(self, deployment_name: str):
        """Show successful destruction message."""
        card = Card(
            content=Text(
                f"Deployment '{deployment_name}' has been successfully destroyed!\n\n"
                "All associated resources have been removed.",
                style=Colors.Ansi.success,
            ),
            title="💀 Destruction Complete",
            border_style=Colors.Ansi.success,
        )
        self.console.print(card)

    def show_cancelled(self):
        """Show cancellation message."""
        card = Card(
            content=Text(
                "Destruction cancelled.\n\nYour deployment is safe!",
                style=Colors.Ansi.warning,
            ),
            title="🛑 Cancelled",
            border_style=Colors.Ansi.warning,
        )
        self.console.print(card)

    def show_error(self, message: str):
        """Show error message."""
        card = Card(
            content=Text(f"{message}", style=Colors.Ansi.error),
            title="💀 Destruction Failed",
            border_style=Colors.Ansi.error,
        )
        self.console.print(card)
