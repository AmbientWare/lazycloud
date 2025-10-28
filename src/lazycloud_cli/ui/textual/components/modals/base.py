from textual.containers import Container
from textual.screen import ModalScreen


class ModalContainer(Container):
    """A styled container for modal content."""

    def __init__(self, title: str = "", **kwargs):
        super().__init__(**kwargs)
        self.modal_title = title

    def on_mount(self) -> None:
        """Apply styling."""
        self.styles.width = "auto"
        self.styles.height = "auto"
        self.styles.padding = 2


class BaseModalScreen(ModalScreen):
    """Base modal screen with common keybindings."""

    BINDINGS = [
        ("escape", "dismiss", "Close"),
    ]

    def on_mount(self) -> None:
        """Style the modal screen."""
        self.styles.align = ("center", "middle")

    def action_dismiss(self) -> None:
        """Close the modal."""
        self.dismiss()
