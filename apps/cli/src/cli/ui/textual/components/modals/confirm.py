from typing import Callable

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from cli.ui.textual.components.modals.base import BaseModalScreen


class ConfirmModal(BaseModalScreen):
    """Reusable confirmation modal component."""

    BINDINGS = [
        ("y", "confirm", "Yes"),
        ("n", "cancel", "No"),
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(
        self,
        title: str,
        message: str,
        on_confirm: Callable | None = None,
        on_cancel: Callable | None = None,
        icon: str = "❓",
    ):
        """Initialize the confirm modal.

        Args:
            title: The title of the modal
            message: The confirmation message to display
            on_confirm: Callback function to execute on confirmation
            on_cancel: Callback function to execute on cancellation
            icon: Icon to display in the title (default: ❓)
        """
        super().__init__()
        self.title = title
        self.message = message
        self.on_confirm_callback = on_confirm
        self.on_cancel_callback = on_cancel
        self.icon = icon

    def compose(self) -> ComposeResult:
        """Create the modal layout."""
        with Vertical(id="confirm-modal"):
            yield Static(
                f"[bold]{self.icon} {self.title}[/bold]",
                id="confirm-header",
            )
            yield Static(
                f"{self.message}",
                id="confirm-message",
            )

    def on_mount(self) -> None:
        """Set border subtitle."""
        super().on_mount()
        modal = self.query_one("#confirm-modal")
        modal.border_subtitle = "y: Confirm • n/Esc: Cancel"

    def action_confirm(self) -> None:
        """Handle confirmation action."""
        if self.on_confirm_callback:
            try:
                result = self.on_confirm_callback()
                self.dismiss(result)
            except Exception:
                self.dismiss(False)
        else:
            self.dismiss(True)

    def action_cancel(self) -> None:
        """Handle cancel action."""
        if self.on_cancel_callback:
            try:
                result = self.on_cancel_callback()
                self.dismiss(result)
            except Exception:
                self.dismiss(False)
        else:
            self.dismiss(False)
