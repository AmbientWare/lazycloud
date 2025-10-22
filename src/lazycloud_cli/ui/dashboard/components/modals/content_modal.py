from textual.app import ComposeResult
from textual.containers import Container, Vertical
from textual.widgets import Static

from lazycloud_cli.ui.dashboard.components.modals import BaseModalScreen
from lazycloud_cli.ui.dashboard.theme import Borders


class ContentModal(BaseModalScreen):
    """Base class for modals with rich content (logs, details, etc)."""

    def __init__(
        self,
        title: str = "",
        subtitle: str = "",
        icon: str = "📄",
        modal_width: str = "90%",
        modal_height: str = "85%",
        border_style: tuple | None = None,
    ):
        """Initialize content modal"""
        super().__init__()
        self.title = title
        self.subtitle = subtitle
        self.icon = icon
        self.modal_width = modal_width
        self.modal_height = modal_height
        self.border_style = border_style or Borders.default

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
        """Apply theme-based styling."""
        super().on_mount()

        # Style the wrapper
        wrapper = self.query_one("#modal-wrapper")
        wrapper.styles.align = ("center", "middle")
        wrapper.styles.width = "100%"
        wrapper.styles.height = "100%"
        wrapper.styles.background = "transparent"

        # Style the container with cleaner border
        container = self.query_one("#content-modal-container")
        container.styles.width = self.modal_width
        container.styles.height = self.modal_height
        container.styles.padding = 2
        container.styles.border = self.border_style
        container.styles.background = "transparent"

        # Style header if it exists
        try:
            header = self.query_one("#modal-header")
            header.styles.dock = "top"
            header.styles.height = 1
            header.styles.margin = (0, 0, 1, 0)
            header.styles.text_align = "center"
        except Exception:
            # No header to style
            pass
