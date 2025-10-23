from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Input, Static

from lazycloud_cli.api import api
from lazycloud_cli.ui.dashboard.components import ConfirmModal, ErrorModal, InputModal
from lazycloud_cli.ui.dashboard.components.modals.base import BaseModalScreen
from shared.models.secrets import SecretCollection


class AddSecretModal(InputModal):
    """Modal for adding a new secret."""

    def __init__(self, deployment_id: str):
        self.deployment_id = deployment_id

        super().__init__(
            title="Add Secret",
            message=(
                "Add a new environment variable:\n\n"
                "[dim]Enter the variable name and value[/dim]"
            ),
            placeholder="VARIABLE_NAME",
            initial_value="",
            on_confirm=self.handle_add,
            icon="➕",
            password=False,
        )
        self._value_entered = False
        self._variable_name = ""

    def handle_add(self, input_value: str) -> bool:
        """Handle the add action."""
        if not input_value.strip():
            return False

        if not self._value_entered:
            # First input is the variable name
            self._variable_name = input_value.strip()
            self._value_entered = True

            # Update the modal to ask for the value
            message_widget = self.query_one("#input-message")
            message_widget.update(
                f"Add a new environment variable:\n\n"
                f"[bold]{self._variable_name}[/bold]\n\n"
                f"[dim]Enter the value for this variable[/dim]"
            )

            # Clear the input for the value
            input_field = self.query_one("#input-field")
            input_field.value = ""
            input_field.placeholder = "Enter value..."

            return False  # Don't dismiss yet, we need the value

        # Second input is the value
        variable_value = input_value.strip()

        try:
            # Create a SecretCollection with the new secret
            secret_collection = SecretCollection(
                added={self._variable_name: variable_value},
                removed=[],
            )

            # Call the API to update
            response = api.secrets.update_secrets(
                deployment_id=self.deployment_id,
                secrets=secret_collection,
            )

            if response:
                return True

            # Show error modal if response is falsy
            error_modal = ErrorModal(
                title="Failed to Add Secret",
                message="Could not add the environment variable.\n\n[dim]Please try again.[/dim]",
            )
            self.app.push_screen(error_modal)
            return False

        except Exception as e:
            error_modal = ErrorModal(
                title="Error Adding Secret",
                message=f"An error occurred while adding the secret:\n\n{str(e)}\n\n[dim]Please try again.[/dim]",
            )
            self.app.push_screen(error_modal)
            return False


class EditSecretModal(InputModal):
    """Simple modal for editing a secret value."""

    def __init__(self, deployment_id: str, secret_key: str, current_value: str):
        self.deployment_id = deployment_id
        self.secret_key = secret_key

        super().__init__(
            title=f"Edit Secret: {secret_key}",
            message=f"Edit the value for [bold]{secret_key}[/bold]:\n\n[dim]Press Enter to save, Esc to cancel[/dim]",
            placeholder="Enter value...",
            initial_value=current_value,
            on_confirm=self.handle_edit,
            icon="✏️",
            password=False,
        )

    def handle_edit(self, input_value: str) -> bool:
        """Handle the edit action."""
        new_value = input_value.strip()

        # If empty or unchanged, cancel
        if not new_value:
            return False

        try:
            # Create a SecretCollection with the updated value
            secret_collection = SecretCollection(
                added={self.secret_key: new_value},
                removed=[],
            )

            # Call the API to update
            response = api.secrets.update_secrets(
                deployment_id=self.deployment_id,
                secrets=secret_collection,
            )

            if response:
                return True

            # Show error modal if response is falsy
            error_modal = ErrorModal(
                title="Failed to Update Secret",
                message="Could not update the environment variable.\n\n[dim]Please try again.[/dim]",
            )
            self.app.push_screen(error_modal)
            return False

        except Exception as e:
            error_modal = ErrorModal(
                title="Error Updating Secret",
                message=f"An error occurred while updating the secret:\n\n{str(e)}\n\n[dim]Please try again.[/dim]",
            )
            self.app.push_screen(error_modal)
            return False


class UpdateSecretModal(BaseModalScreen):
    """Modal for viewing/updating a secret value."""

    BINDINGS = [
        ("e", "enable_edit", "Edit"),
        ("escape", "dismiss", "Cancel"),
    ]

    def __init__(self, deployment_id: str, secret_key: str, current_value: str = ""):
        super().__init__()
        self.deployment_id = deployment_id
        self.secret_key = secret_key
        self.current_value = current_value
        self._edit_mode = False

    def compose(self) -> ComposeResult:
        """Create the modal layout."""
        with Vertical(id="view-secret-modal"):
            yield Static(
                "[bold]🔐 View Secret[/bold]",
                id="view-secret-header",
            )
            yield Static(
                f"Variable: [bold]{self.secret_key}[/bold]",
                id="view-secret-key",
            )
            yield Input(
                value=self.current_value,
                placeholder="Secret value...",
                id="secret-input",
            )

    def on_mount(self) -> None:
        """Set up the modal when mounted."""
        super().on_mount()
        modal = self.query_one("#view-secret-modal")
        modal.border_subtitle = "e: Edit • Esc: Close"

        # Make input read-only by default
        input_field = self.query_one("#secret-input", Input)
        input_field.disabled = True

    def action_enable_edit(self) -> None:
        """Enable edit mode."""
        if self._edit_mode:
            # Already in edit mode, try to submit
            self._submit()
        else:
            # Enter edit mode
            self._edit_mode = True
            input_field = self.query_one("#secret-input", Input)
            input_field.disabled = False
            input_field.focus()

            # Update header and subtitle
            header = self.query_one("#view-secret-header")
            header.update("[bold]🔐 Edit Secret[/bold]")

            modal = self.query_one("#view-secret-modal")
            modal.border_subtitle = "Enter: Save • Esc: Cancel"

            # Add enter binding for submit
            self.BINDINGS = [
                ("enter", "submit", "Save"),
                ("escape", "dismiss", "Cancel"),
            ]

    def action_submit(self) -> None:
        """Submit the changes."""
        self._submit()

    def _submit(self) -> None:
        """Handle the update action."""
        input_field = self.query_one("#secret-input", Input)
        new_value = input_field.value.strip()

        # If empty or unchanged, just close
        if not new_value or new_value == self.current_value:
            self.dismiss(False)
            return

        try:
            # Create a SecretCollection with the updated value
            secret_collection = SecretCollection(
                added={self.secret_key: new_value},
                removed=[],
            )

            # Call the API to update
            response = api.secrets.update_secrets(
                deployment_id=self.deployment_id,
                secrets=secret_collection,
            )

            if response:
                self.dismiss(True)
                return

            # Show error modal if response is falsy
            error_modal = ErrorModal(
                title="Failed to Update Secret",
                message="Could not update the environment variable.\n\n[dim]Please try again.[/dim]",
            )
            self.app.push_screen(error_modal)
            self.dismiss(False)

        except Exception as e:
            error_modal = ErrorModal(
                title="Error Updating Secret",
                message=f"An error occurred while updating the secret:\n\n{str(e)}\n\n[dim]Please try again.[/dim]",
            )
            self.app.push_screen(error_modal)
            self.dismiss(False)

    def action_dismiss(self) -> None:
        """Dismiss the modal."""
        self.dismiss(False)


class DeleteSecretModal(ConfirmModal):
    """Modal for confirming secret deletion."""

    def __init__(self, deployment_id: str, secret_key: str):
        self.deployment_id = deployment_id
        self.secret_key = secret_key

        super().__init__(
            title="Delete Secret",
            message=(
                f"Are you sure you want to delete this environment variable?\n\n"
                f"[bold]{secret_key}[/bold]\n\n"
                f"[dim]This action cannot be undone.[/dim]"
            ),
            on_confirm=self.handle_delete,
            icon="🗑️ ",  # extra space needed for alignment
        )

    def handle_delete(self) -> bool:
        """Handle the delete action."""
        try:
            # Create a SecretCollection with the secret to remove
            secret_collection = SecretCollection(
                added={},
                removed=[self.secret_key],
            )

            # Call the API to update (remove the secret)
            response = api.secrets.update_secrets(
                deployment_id=self.deployment_id,
                secrets=secret_collection,
            )

            if response:
                return True

            # Show error modal if response is falsy
            error_modal = ErrorModal(
                title="Failed to Delete Secret",
                message="Could not delete the environment variable.\n\n[dim]Please try again.[/dim]",
            )
            self.app.push_screen(error_modal)
            return False

        except Exception as e:
            error_modal = ErrorModal(
                title="Error Deleting Secret",
                message=f"An error occurred while deleting the secret:\n\n{str(e)}\n\n[dim]Please try again.[/dim]",
            )
            self.app.push_screen(error_modal)
            return False
