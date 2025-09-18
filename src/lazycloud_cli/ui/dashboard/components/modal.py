from textual.containers import Container
from textual.screen import ModalScreen

from lazycloud_cli.ui.dashboard.theme import theme


class ModalContainer(Container):
    """A styled container for modal content."""

    def __init__(self, title: str = "", **kwargs):
        super().__init__(**kwargs)
        self.modal_title = title

    def on_mount(self) -> None:
        """Style the modal container."""
        self.styles.width = "90%"
        self.styles.height = "90%"
        self.styles.border = (theme.border_style, theme.primary)
        self.styles.background = theme.background
        self.styles.padding = 1


class BaseModalScreen(ModalScreen):
    """Base modal screen with common keybindings."""

    DEFAULT_CSS = """
    BaseModalScreen {
        align: center middle;
    }
    """

    BINDINGS = [
        ("escape", "dismiss", "Close"),
        ("q", "dismiss", "Close"),
    ]

    def action_dismiss(self) -> None:
        """Close the modal."""
        self.dismiss()
