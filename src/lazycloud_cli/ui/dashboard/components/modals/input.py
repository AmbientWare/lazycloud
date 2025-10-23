import inspect
from typing import Callable

from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Input, Static

from lazycloud_cli.ui.dashboard.components.modals.base import BaseModalScreen


class InputModal(BaseModalScreen):
    """Reusable input modal component."""

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(
        self,
        title: str,
        message: str,
        placeholder: str = "",
        initial_value: str = "",
        on_confirm: Callable[[str], bool] | None = None,
        on_cancel: Callable | None = None,
        icon: str = "✏️",
        password: bool = False,
    ):
        """Initialize the input modal"""
        super().__init__()
        self.title = title
        self.message = message
        self.placeholder = placeholder
        self.initial_value = initial_value
        self.on_confirm_callback = on_confirm
        self.on_cancel_callback = on_cancel
        self.icon = icon
        self.password = password
        self._input = None

    def compose(self) -> ComposeResult:
        """Create the modal layout."""
        with Vertical(id="input-modal"):
            yield Static(
                f"[bold]{self.icon} {self.title}[/bold]",
                id="input-header",
            )
            yield Static(
                f"{self.message}",
                id="input-message",
            )
            input_widget = Input(
                placeholder=self.placeholder,
                value=self.initial_value,
                password=self.password,
                validate_on=["submitted"],
                id="input-field",
            )
            self._input = input_widget
            yield input_widget

    def on_mount(self) -> None:
        """Set border subtitle and focus input."""
        super().on_mount()
        modal = self.query_one("#input-modal")
        modal.border_subtitle = "↵ Submit • Esc: Cancel"

        # Focus the input field
        input_field = self.query_one("#input-field", Input)
        input_field.focus()

    @on(Input.Submitted)
    async def handle_input_submit(self, event: Input.Submitted) -> None:
        """Handle when Enter is pressed in the input field."""
        # Get the value from the event
        value = event.value

        if self.on_confirm_callback:
            # change input for debugging
            success = self.on_confirm_callback(value)
            if inspect.isawaitable(success):
                success = await success
            if success:
                self.dismiss(True)
        else:
            self.dismiss(True)

    def action_cancel(self) -> None:
        """Handle cancellation action."""
        if self.on_cancel_callback:
            self.on_cancel_callback()
        self.dismiss(False)
