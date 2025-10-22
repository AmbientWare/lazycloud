from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from lazycloud_cli.ui.dashboard.components.modals.base import BaseModalScreen


class SuccessModal(BaseModalScreen):
    BINDINGS = [
        ("enter", "dismiss", "Close"),
        ("q", "dismiss", "Close"),
        ("escape", "dismiss", "Close"),
    ]

    def __init__(
        self,
        title: str,
        message: str,
        icon: str = "✓",
    ):
        super().__init__()
        self.title = title
        self.message = message
        self.icon = icon

    def compose(self) -> ComposeResult:
        """Create the modal layout."""
        with Vertical(id="success-modal"):
            yield Static(
                f"[bold]{self.icon} {self.title}[/bold]",
                id="success-header",
            )
            yield Static(
                f"{self.message}",
                id="success-message",
            )

    def on_mount(self) -> None:
        """Set border subtitle."""
        super().on_mount()
        modal = self.query_one("#success-modal")
        modal.border_subtitle = "Enter/q: Close"

    def action_dismiss(self) -> None:
        """Dismiss the success modal."""
        self.dismiss()
