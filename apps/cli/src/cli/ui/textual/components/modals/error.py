from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from cli.ui.textual.components.modals.base import BaseModalScreen


class ErrorModal(BaseModalScreen):
    """Modal for displaying error messages."""

    BINDINGS = [
        ("enter", "dismiss", "Close"),
        ("escape", "dismiss", "Close"),
    ]

    def __init__(
        self,
        title: str,
        message: str,
        icon: str = "✗",
    ):
        super().__init__()
        self.title = title
        self.message = message
        self.icon = icon

    def compose(self) -> ComposeResult:
        """Create the modal layout."""
        with Vertical(id="error-modal"):
            yield Static(
                f"[bold red]{self.icon} {self.title}[/bold red]",
                id="error-header",
            )
            yield Static(
                f"{self.message}",
                id="error-message",
            )

    def on_mount(self) -> None:
        """Set border subtitle."""
        super().on_mount()
        modal = self.query_one("#error-modal")
        modal.border_subtitle = "Enter/Esc: Close"

    def action_dismiss(self) -> None:
        """Dismiss the error modal."""
        self.dismiss()
