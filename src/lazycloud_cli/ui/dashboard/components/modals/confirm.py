from typing import Callable

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from lazycloud_cli.ui.dashboard.components.modals.base import BaseModalScreen
from lazycloud_cli.ui.dashboard.theme import theme


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
        border_color: str | None = None,
    ):
        """Initialize the confirm modal.

        Args:
            title: The title of the modal
            message: The confirmation message to display
            on_confirm: Callback function to execute on confirmation
            on_cancel: Callback function to execute on cancellation
            icon: Icon to display in the title (default: ❓)
            border_color: Border color from theme (default: theme.warning)
        """
        super().__init__()
        self.title = title
        self.message = message
        self.on_confirm_callback = on_confirm
        self.on_cancel_callback = on_cancel
        self.icon = icon
        self.border_color = border_color or theme.warning

    def compose(self) -> ComposeResult:
        """Create the modal layout."""
        with Vertical(id="confirm-modal"):
            yield Static(
                f"[bold]{self.icon} {self.title}[/bold]",
                id="confirm-header",
            )
            yield Static(
                f"{self.message}\n\n"
                f"Press [bold green]Y[/bold green] to confirm or [bold red]N[/bold red] to cancel",
                id="confirm-message",
            )

    def on_mount(self) -> None:
        """Apply theme-based styling using Python."""
        # Call parent on_mount
        super().on_mount()

        # Style the modal container
        modal = self.query_one("#confirm-modal")
        modal.styles.width = 60
        modal.styles.height = "auto"
        modal.styles.max_height = 20
        modal.styles.padding = 2
        modal.styles.background = theme.background
        modal.styles.border = (theme.border_style, self.border_color)

        # Style header
        header = self.query_one("#confirm-header")
        header.styles.text_align = "center"
        header.styles.margin = (0, 0, 1, 0)

        # Style message
        message = self.query_one("#confirm-message")
        message.styles.text_align = "center"
        message.styles.margin = (1, 2, 1, 2)

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
