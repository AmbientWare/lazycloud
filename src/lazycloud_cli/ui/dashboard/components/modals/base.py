from textual.containers import Container
from textual.screen import ModalScreen

from lazycloud_cli.ui.dashboard.theme import theme


class ModalContainer(Container):
    """A styled container for modal content."""

    def __init__(self, title: str = "", **kwargs):
        super().__init__(**kwargs)
        self.modal_title = title

    def on_mount(self) -> None:
        """Apply theme-based styling."""
        self.styles.width = "auto"
        self.styles.height = "auto"
        self.styles.padding = 2
        self.styles.background = theme.background
        self.styles.border = (theme.border_style, theme.primary)


class BaseModalScreen(ModalScreen):
    """Base modal screen with common keybindings."""

    BINDINGS = [
        ("escape", "dismiss", "Close"),
        ("q", "dismiss", "Close"),
    ]

    def on_mount(self) -> None:
        """Style the modal screen."""
        self.styles.align = ("center", "middle")

    def action_dismiss(self) -> None:
        """Close the modal."""
        self.dismiss()
