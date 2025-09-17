"""Confirmation dialog component for user confirmations."""

from typing import List, Optional

from rich.console import Console
from rich.prompt import Confirm
from rich.text import Text

from lazycloud_cli.ui.components.card import Card
from lazycloud_cli.ui.theme import theme


class ConfirmationDialog:
    """A confirmation dialog that shows a card with details before prompting."""

    def __init__(
        self,
        title: str,
        question: str,
        details: Optional[List[str]] = None,
        warning_message: Optional[str] = None,
        danger: bool = False,
        default: bool = False,
    ):
        self.title = title
        self.question = question
        self.details = details or []
        self.warning_message = warning_message
        self.danger = danger
        self.default = default

    def _create_card(self) -> Card:
        """Create the confirmation details card."""
        content = Text()

        # Add details as bullet points
        if self.details:
            for loc, detail in enumerate(self.details):
                content.append("• ", style=theme.text_secondary)
                content.append(f"{detail}", style=theme.text_primary)
                if not loc == len(self.details) - 1:
                    content.append("\n")

        # Add warning message if provided
        if self.warning_message:
            if self.details:
                content.append("\n")
            style = theme.warning if not self.danger else theme.error
            content.append(self.warning_message, style=style)

        # Determine border style
        border_style = (
            theme.border_error
            if self.danger
            else theme.border_warning
            if self.warning_message
            else theme.border_primary
        )

        return Card(
            content=content,
            title=self.title,
            border_style=border_style,
        )

    def show(self, console: Console) -> bool:
        """Show the confirmation dialog and return the user's choice.

        Args:
            console: Rich console instance

        Returns:
            True if confirmed, False otherwise
        """
        # Show the details card
        card = self._create_card()
        console.print(card)

        # Style the question based on danger level
        question_style = "bold red" if self.danger else "bold yellow"

        # Ask for confirmation
        return Confirm.ask(
            f"[{question_style}]{self.question}[/{question_style}]",
            default=self.default,
        )


class DestructiveConfirmationDialog(ConfirmationDialog):
    """Specialized confirmation for destructive actions."""

    def __init__(
        self,
        resource_type: str,
        resource_name: str,
        consequences: Optional[List[str]] = None,
        custom_warning: Optional[str] = None,
    ):
        """Initialize destructive confirmation dialog.

        Args:
            resource_type: Type of resource (e.g., "deployment", "service")
            resource_name: Name of the resource
            consequences: List of consequences of the action
            custom_warning: Custom warning message (defaults to "cannot be undone")
        """
        title = f"🚨 Confirm {resource_type.title()} Deletion"
        question = f"Are you sure you want to delete '{resource_name}'?"

        # Default consequences if none provided
        if consequences is None:
            consequences = [
                f"The {resource_type} will be permanently deleted",
                "All associated resources will be removed",
                "This action cannot be undone",
            ]

        warning = custom_warning or "This action cannot be undone!"

        super().__init__(
            title=title,
            question=question,
            details=consequences,
            warning_message=warning,
            danger=True,
            default=False,
        )


class SimpleConfirmationDialog(ConfirmationDialog):
    """Simple confirmation for non-destructive actions."""

    def __init__(
        self,
        action: str,
        details: Optional[List[str]] = None,
        title: Optional[str] = None,
    ):
        dialog_title = title or "Confirmation Required"
        question = f"Do you want to {action}?"

        super().__init__(
            title=dialog_title,
            question=question,
            details=details,
            danger=False,
            default=True,
        )


def confirm_action(
    console: Console,
    action: str,
    resource_name: Optional[str] = None,
    details: Optional[List[str]] = None,
    danger: bool = False,
) -> bool:
    """Convenience function for quick confirmations"""
    if danger:
        # Use destructive confirmation
        if not resource_name:
            resource_name = "this resource"
        dialog = DestructiveConfirmationDialog(
            resource_type="resource",
            resource_name=resource_name,
            consequences=details,
        )
    else:
        # Use simple confirmation
        if resource_name:
            action = f"{action} '{resource_name}'"
        dialog = SimpleConfirmationDialog(
            action=action,
            details=details,
        )

    return dialog.show(console)
