from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from lazycloud_cli.ui.dashboard.components.modals.base import BaseModalScreen
from lazycloud_cli.ui.dashboard.theme import theme


class SuccessModal(BaseModalScreen):
    """Reusable success modal component."""

    BINDINGS = [
        ("enter", "dismiss", "Close"),
        ("escape", "dismiss", "Close"),
    ]

    def __init__(
        self,
        title: str,
        message: str,
        icon: str = "✓",
        border_color: str | None = None,
    ):
        super().__init__()
        self.title = title
        self.message = message
        self.icon = icon
        self.border_color = border_color or theme.success

    def compose(self) -> ComposeResult:
        """Create the modal layout."""
        with Vertical(id="success-modal"):
            yield Static(
                f"[bold green]{self.icon} {self.title}[/bold green]",
                id="success-header",
            )
            yield Static(
                f"{self.message}\n\n[dim]Press Enter to close.[/dim]",
                id="success-message",
            )

    def on_mount(self) -> None:
        """Apply theme-based styling using Python."""
        super().on_mount()
        modal = self.query_one("#success-modal")
        modal.styles.width = 60
        modal.styles.height = "auto"
        modal.styles.max_height = 15
        modal.styles.padding = 2
        modal.styles.background = theme.background
        modal.styles.border = (theme.border_style, self.border_color)

        header = self.query_one("#success-header")
        header.styles.text_align = "center"
        header.styles.margin = (0, 0, 1, 0)

        message = self.query_one("#success-message")
        message.styles.text_align = "center"
        message.styles.margin = (1, 2, 1, 2)

    def action_dismiss(self) -> None:
        """Dismiss the success modal."""
        self.dismiss()
