"""Destroy command view components."""

from typing import Any, Dict

from rich.console import Console
from rich.text import Text

from lazycloud_cli.ui.components.card import Card
from lazycloud_cli.ui.components.confirmation import DestructiveConfirmationDialog
from lazycloud_cli.ui.components.progress import SpinnerProgress
from lazycloud_cli.ui.theme import theme


class DestroyTargetCard(Card):
    """Card showing the deployment targeted for destruction."""

    def __init__(self, deployment: Dict[str, Any]):
        """Initialize destroy target card."""
        content = Text()
        content.append("You are about to destroy:\n\n", style=theme.warning)
        content.append("  Deployment: ", style=theme.text_secondary)
        content.append(f"{deployment['name']}\n", style=f"{theme.highlight} bold")

        if deployment.get("services_count"):
            content.append("  Services:   ", style=theme.text_secondary)
            content.append(
                f"{deployment['services_count']} services\n", style=theme.warning
            )

        content.append("\n", style=f"{theme.error} bold")
        content.append("This action cannot be undone!", style=theme.error)

        super().__init__(
            content=content,
            title="🚨 Destruction Target",
            border_style=theme.border_error,
        )


class DestroyConfirmationCard(Card):
    """Card for confirming destruction."""

    def __init__(self, deployment_name: str):
        """Initialize confirmation card."""
        content = Text()
        content.append("This will permanently delete:\n\n", style=theme.warning)
        content.append("  • All deployed services\n", style=theme.text_secondary)
        content.append("  • All running containers\n", style=theme.text_secondary)
        content.append("  • All configuration data\n", style=theme.text_secondary)
        content.append("  • All persistent storage\n", style=theme.text_secondary)
        content.append("  • All network configurations\n", style=theme.text_secondary)

        super().__init__(
            content=content,
            title="⚠️  Confirm Destruction",
            border_style=theme.border_warning,
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

        # Use the destructive confirmation dialog
        dialog = DestructiveConfirmationDialog(
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

    def show_progress(self, deployment_name: str) -> SpinnerProgress:
        """Show destruction progress.

        Args:
            deployment_name: Name of deployment being destroyed

        Returns:
            SpinnerProgress instance for updates
        """
        return SpinnerProgress(f"Destroying deployment '{deployment_name}'...")

    def show_success(self, deployment_name: str):
        """Show successful destruction message."""
        card = Card(
            content=Text(
                f"Deployment '{deployment_name}' has been successfully destroyed!\n\n"
                "All associated resources have been removed.",
                style=theme.success,
            ),
            title="💀 Destruction Complete",
            border_style=theme.border_success,
        )
        self.console.print(card)

    def show_cancelled(self):
        """Show cancellation message."""
        card = Card(
            content=Text(
                "🛑 Destruction cancelled.\n\nYour deployment is safe!",
                style=theme.warning,
            ),
            title="🛑 Cancelled",
            border_style=theme.border_warning,
        )
        self.console.print(card)

    def show_error(self, message: str):
        """Show error message."""
        card = Card(
            content=Text(f"{message}", style=theme.error),
            title="💀 Destruction Failed",
            border_style=theme.border_error,
        )
        self.console.print(card)
