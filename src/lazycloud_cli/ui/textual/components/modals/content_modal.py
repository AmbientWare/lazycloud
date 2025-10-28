from textual.app import ComposeResult
from textual.containers import Container, Vertical
from textual.widgets import Static

from lazycloud_cli.ui.textual.components.modals import BaseModalScreen


class ContentModal(BaseModalScreen):
    """Base class for modals with rich content (logs, details, etc)."""

    def __init__(
        self,
        title: str = "",
        subtitle: str = "",
        icon: str = "📄",
        modal_width: str = "90%",
        modal_height: str = "85%",
        border_class: str = "modal-default",
    ):
        """Initialize content modal"""
        super().__init__()
        self.title = title
        self.subtitle = subtitle
        self.icon = icon
        self.modal_width = modal_width
        self.modal_height = modal_height
        self.border_class = border_class

    def compose(self) -> ComposeResult:
        """Create the modal layout."""
        with Container(id="modal-wrapper"):
            with Vertical(id="content-modal-container"):
                # Only show header if title/icon provided
                if self.title or self.icon:
                    header_text = f"[bold]{self.icon} {self.title}[/bold]"
                    if self.subtitle:
                        header_text += f" [dim]{self.subtitle}[/dim]"
                    yield Static(header_text, id="modal-header")

                # Body content (to be filled by subclasses)
                yield from self.compose_body()

    def compose_body(self) -> ComposeResult:
        """Override this to provide the modal body content."""
        yield Static("Override compose_body() to add content")

    def on_mount(self) -> None:
        """Apply sizing and add CSS class for border styling."""
        super().on_mount()

        container = self.query_one("#content-modal-container")
        container.styles.width = self.modal_width
        container.styles.height = self.modal_height

        container.add_class(self.border_class)
