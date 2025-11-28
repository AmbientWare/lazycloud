from typing import Any, Dict

from rich.console import Console
from rich.text import Text

from cli.ui.colors import Colors
from cli.ui.components.card import Card
from cli.ui.components.confirmation import (
    StringValidationConfirmationDialog,
)
from cli.ui.components.info_cards import (
    ActionProgressCard,
    ErrorCard,
    NotFoundCard,
    SuccessDetailsCard,
    WarningCard,
)


class WorkspaceView:
    """Main view orchestrator for workspace commands."""

    def __init__(self, console: Console):
        """Initialize the workspace view."""
        self.console = console

    def show_creating(self, name: str):
        """Show workspace creation in progress."""
        card = ActionProgressCard(
            action="Creating workspace",
            resource_name=name,
            icon="🗂️ ",  # extra space to align with the icon
        )
        self.console.print(card)

    def show_created(self, workspace: Dict[str, Any]):
        """Show successful workspace creation."""
        card = SuccessDetailsCard(
            title="🗂️  Workspace Created",  # extra space to align with the icon
            message="Workspace created successfully!",
            details={"Name": workspace.get("name", "")},
        )
        self.console.print(card)

    def show_activated(self, workspace: Dict[str, Any]):
        """Show successful workspace activation."""
        card = SuccessDetailsCard(
            title="🗂️  Active Workspace",  # extra space to align with the icon
            message="Workspace activated!",
            details={
                "Name": workspace.get("name", ""),
                "Role": workspace.get("role", "unknown"),
            },
        )
        self.console.print(card)

    def show_not_found(self, name: str):
        """Show workspace not found error."""
        card = NotFoundCard(
            resource_type="Workspace", resource_name=name, icon="🗂️ "
        )  # extra space to align with the icon
        self.console.print(card)

    def show_personal_cannot_remove(self):
        """Show error when trying to remove personal workspace."""
        card = Card(
            content=Text(
                "Cannot remove personal workspace.\n\n"
                "Personal workspaces are permanent and cannot be deleted.",
                style=Colors.Ansi.error,
            ),
            title="🗂️  Cannot Remove",  # extra space to align with the icon
            border_style=Colors.Ansi.error,
        )
        self.console.print(card)

    def confirm_removal(self, workspace_name: str, force: bool = False) -> bool:
        """Show confirmation prompt for workspace removal"""
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
        card = SuccessDetailsCard(
            title="🗂️  Workspace Removed",  # extra space to align with the icon
            message=f"Workspace '{workspace_name}' has been successfully removed.\n\nAll associated resources have been deleted.",
        )
        self.console.print(card)

    def show_cancelled(self):
        """Show cancellation message."""
        card = WarningCard(
            message="Operation cancelled.",
            title="🗂️  Cancelled",  # extra space to align with the icon
        )
        self.console.print(card)

    def show_error(self, message: str, suggestion: str | None = None):
        """Show error message.

        Args:
            message: The error message
            suggestion: Optional suggestion for fixing the error
        """
        card = ErrorCard(
            message=message,
            title="🗂️  Error",  # extra space to align with the icon
            suggestion=suggestion,
        )
        self.console.print(card)
