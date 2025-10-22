from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.text import Text

from lazycloud_cli.ui.colors import Colors
from lazycloud_cli.ui.components.card import Card


class ConfirmationDialog:
    """A confirmation dialog that shows a card with details before prompting."""

    def __init__(
        self,
        title: str,
        question: str,
        details: list[str] | None = None,
        warning_message: str | None = None,
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
                content.append("• ", style=Colors.Ansi.text_muted)
                content.append(f"{detail}", style=Colors.Ansi.text)
                if not loc == len(self.details) - 1:
                    content.append("\n")

        # Add warning message if provided
        if self.warning_message:
            if self.details:
                content.append("\n")
            style = Colors.Ansi.warning if not self.danger else Colors.Ansi.error
            content.append(self.warning_message, style=style)

        # Determine border style
        border_style = (
            Colors.Ansi.error
            if self.danger
            else Colors.Ansi.warning
            if self.warning_message
            else Colors.Ansi.primary
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
        result = Confirm.ask(
            f"[{question_style}]{self.question}[/{question_style}]",
            default=self.default,
        )

        # Print a newline after confirmation for better spacing
        console.print()

        return result


class DestructiveConfirmationDialog(ConfirmationDialog):
    """Specialized confirmation for destructive actions."""

    def __init__(
        self,
        resource_type: str,
        resource_name: str,
        consequences: list[str] | None = None,
        custom_warning: str | None = None,
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


class StringValidationConfirmationDialog:
    """Confirmation dialog that requires typing the resource name to confirm."""

    def __init__(
        self,
        resource_type: str,
        resource_name: str,
        consequences: list[str] | None = None,
        custom_warning: str | None = None,
    ):
        """Initialize string validation confirmation dialog.

        Args:
            resource_type: Type of resource (e.g., "workspace", "deployment")
            resource_name: Name of the resource that must be typed to confirm
            consequences: List of consequences of the action
            custom_warning: Custom warning message
        """
        self.resource_type = resource_type
        self.resource_name = resource_name
        self.consequences = consequences or [
            f"The {resource_type} will be permanently deleted",
            "All associated resources will be removed",
            "This action cannot be undone",
        ]
        self.warning = custom_warning or "This action cannot be undone!"
        self.title = f"🚨 Confirm {resource_type.title()} Deletion"

    def _create_card(self) -> Card:
        """Create the confirmation details card."""
        content = Text()

        # Add consequences as bullet points
        for loc, consequence in enumerate(self.consequences):
            content.append("• ", style=Colors.Ansi.text_muted)
            content.append(f"{consequence}", style=Colors.Ansi.text)
            if not loc == len(self.consequences) - 1:
                content.append("\n")

        # Add warning message
        if self.warning:
            content.append("\n")
            content.append(self.warning, style=Colors.Ansi.error)

        return Card(
            content=content,
            title=self.title,
            border_style=Colors.Ansi.error,
        )

    def show(self, console: Console) -> bool:
        """Show the confirmation dialog and return the user's choice.

        Args:
            console: Rich console instance

        Returns:
            True if confirmed (typed correctly), False otherwise
        """
        # Show the details card
        card = self._create_card()
        console.print(card)

        # Prompt for string validation
        console.print(
            f"[bold red]To confirm, type the {self.resource_type} name:[/bold red] [bold]{self.resource_name}[/bold]"
        )

        user_input = Prompt.ask("[bold yellow]>>[/bold yellow]")

        # Print newline for spacing
        console.print()

        # Validate input
        if user_input.strip() == self.resource_name:
            return True
        else:
            console.print(
                f"[{Colors.Ansi.error}]✗ Name doesn't match. Deletion cancelled.[/{Colors.Ansi.error}]"
            )
            console.print()
            return False


class SimpleConfirmationDialog(ConfirmationDialog):
    """Simple confirmation for non-destructive actions."""

    def __init__(
        self,
        action: str,
        details: list[str] | None = None,
        title: str | None = None,
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
    resource_name: str | None = None,
    details: list[str] | None = None,
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
