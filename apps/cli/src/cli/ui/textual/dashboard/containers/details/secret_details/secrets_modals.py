from models.secrets import Secret, SecretSource

from cli.api import api
from cli.ui.textual.components import ConfirmModal, ErrorModal, InputModal


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
            secret = Secret(
                key=self._variable_name,
                value=variable_value,
                source=SecretSource.USER,
            )

            # Call the API to create the new secret
            response = api.secrets.store_secrets(
                deployment_id=self.deployment_id,
                secrets=[secret],
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

    def __init__(
        self,
        deployment_id: str,
        secret_key: str,
        current_value: str,
        source: SecretSource = SecretSource.USER,
    ):
        self.deployment_id = deployment_id
        self.secret_key = secret_key
        self.source = source

        super().__init__(
            title=f"Edit Secret: {secret_key}",
            message=f"Edit the value for [bold]{secret_key}[/bold]:\n\n[dim]Press Enter to save, Esc to cancel[/dim]",
            placeholder="Enter value...",
            initial_value=current_value,
            on_confirm=self.handle_edit,
            icon="✏️ ",  # extra space needed for alignment
            password=False,
        )

    def handle_edit(self, input_value: str) -> bool:
        """Handle the edit action."""
        new_value = input_value.strip()

        # If empty or unchanged, cancel
        if not new_value:
            return False

        try:
            # Create secret with the updated value
            secret = Secret(
                key=self.secret_key,
                value=new_value,
                source=self.source,
            )

            # Call the API to update
            response = api.secrets.update_secrets(
                deployment_id=self.deployment_id,
                secrets=[secret],
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


class DeleteSecretModal(ConfirmModal):
    """Modal for confirming secret deletion."""

    def __init__(
        self,
        deployment_id: str,
        secret_key: str,
        source: SecretSource = SecretSource.USER,
    ):
        self.deployment_id = deployment_id
        self.secret_key = secret_key
        self.source = source

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
            # Create secret to remove
            secret_to_remove = Secret(
                key=self.secret_key,
                value="",
                source=self.source,
            )

            response = api.secrets.delete_secrets(
                deployment_id=self.deployment_id,
                secrets=[secret_to_remove],
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
