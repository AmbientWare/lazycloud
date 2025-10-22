from typing import Any, Dict

from rich.console import Console
from rich.text import Text

from lazycloud_cli.ui.colors import Colors
from lazycloud_cli.ui.components.card import Card
from lazycloud_cli.ui.components.confirmation import (
    StringValidationConfirmationDialog,
)


class WorkspaceView:
    """Main view orchestrator for workspace commands."""

    def __init__(self, console: Console):
        """Initialize the workspace view."""
        self.console = console

    def show_creating(self, name: str):
        """Show workspace creation in progress."""
        card = Card(
            content=Text(f"Creating workspace '{name}'...", style=Colors.Ansi.info),
            title="🗂️ Creating Workspace",
            border_style=Colors.Ansi.info,
        )
        self.console.print(card)

    def show_created(self, workspace: Dict[str, Any]):
        """Show successful workspace creation."""
        content = Text()
        content.append(
            "✓ Workspace created successfully!\n\n", style=Colors.Ansi.success
        )
        content.append("  Name: ", style=Colors.Ansi.text_muted)
        content.append(f"{workspace.get('name', '')}", style=Colors.Ansi.primary)

        card = Card(
            content=content,
            title="🗂️ Workspace Created",
            border_style=Colors.Ansi.success,
        )
        self.console.print(card)

    def show_activated(self, workspace: Dict[str, Any]):
        """Show successful workspace activation."""
        content = Text()
        content.append("✓ Workspace activated!\n\n", style=Colors.Ansi.success)
        content.append("  Name: ", style=Colors.Ansi.text_muted)
        content.append(f"{workspace.get('name', '')}\n", style=Colors.Ansi.primary)
        content.append("  Role: ", style=Colors.Ansi.text_muted)
        content.append(
            f"{workspace.get('role', 'unknown')}", style=Colors.Ansi.text_muted
        )

        card = Card(
            content=content,
            title="🗂️ Active Workspace",
            border_style=Colors.Ansi.success,
        )
        self.console.print(card)

    def show_not_found(self, name: str):
        """Show workspace not found error."""
        card = Card(
            content=Text(f"Workspace '{name}' not found", style=Colors.Ansi.error),
            title="🗂️ Not Found",
            border_style=Colors.Ansi.error,
        )
        self.console.print(card)

    def show_personal_cannot_remove(self):
        """Show error when trying to remove personal workspace."""
        card = Card(
            content=Text(
                "Cannot remove personal workspace.\n\n"
                "Personal workspaces are permanent and cannot be deleted.",
                style=Colors.Ansi.error,
            ),
            title="🗂️ Cannot Remove",
            border_style=Colors.Ansi.error,
        )
        self.console.print(card)

    def confirm_removal(self, workspace_name: str, force: bool = False) -> bool:
        """Show confirmation prompt for workspace removal.

        Args:
            workspace_name: Name of workspace to remove
            force: Skip confirmation if True

        Returns:
            True if confirmed, False otherwise
        """
        if force:
            return True

        dialog = StringValidationConfirmationDialog(
            resource_type="workspace",
            resource_name=workspace_name,
            consequences=[
                "All workspace members will lose access",
                "All deployments in this workspace will be deleted",
                "This action cannot be undone",
            ],
        )

        return dialog.show(self.console)

    def show_removed(self, workspace_name: str):
        """Show successful workspace removal."""
        card = Card(
            content=Text(
                f"Workspace '{workspace_name}' has been successfully removed.\n\n"
                "All associated resources have been deleted.",
                style=Colors.Ansi.success,
            ),
            title="🗂️ Workspace Removed",
            border_style=Colors.Ansi.success,
        )
        self.console.print(card)

    def show_cancelled(self):
        """Show cancellation message."""
        card = Card(
            content=Text(
                "Operation cancelled.",
                style=Colors.Ansi.warning,
            ),
            title="🗂️ Cancelled",
            border_style=Colors.Ansi.warning,
        )
        self.console.print(card)

    def show_error(self, message: str, suggestion: str | None = None):
        """Show error message.

        Args:
            message: The error message
            suggestion: Optional suggestion for fixing the error
        """
        content = Text()
        content.append(f"{message}", style=Colors.Ansi.error)

        if suggestion:
            content.append("\n\n", style="")
            content.append("Suggestion: ", style=f"bold {Colors.Ansi.info}")
            content.append(suggestion, style=Colors.Ansi.text_muted)

        card = Card(
            content=content,
            title="🗂️ Error",
            border_style=Colors.Ansi.error,
        )
        self.console.print(card)
