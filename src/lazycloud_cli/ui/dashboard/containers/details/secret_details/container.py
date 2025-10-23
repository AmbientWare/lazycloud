from textual.message import Message
from textual.widgets import DataTable

from lazycloud_cli.api import api
from lazycloud_cli.ui.dashboard.containers.details.secret_details.secrets_modals import (
    AddSecretModal,
    DeleteSecretModal,
    EditSecretModal,
)

# Maximum length for displaying secret values before truncating
MAX_DISPLAY_LENGTH = 25


class SecretsTable(DataTable):
    """Custom DataTable for secrets that handles its own events."""

    BINDINGS = [
        ("a", "add_secret", "Add Secret"),
        ("e", "edit_secret", "Edit Secret"),
        ("d", "delete_secret", "Delete Secret"),
        ("s", "show_secret", "Show Secret"),
    ]

    def __init__(self, deployment_id: str, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.deployment_id = deployment_id
        self.selected_secret_key = None
        self.can_focus = True
        self.cursor_type = "row"
        self.show_cursor = True
        self._secrets = {}
        self._showing_values = False

        self.add_columns("Secret Name", "Value")

    def on_mount(self) -> None:
        """Initialize the table on mount."""
        self.styles.height = "auto"
        self.zebra_stripes = True
        self.show_row_labels = False
        self.border_subtitle = "a: Add • s: Show All/Hide All • e: Edit • d: Delete"

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Handle row highlight events."""
        if event.row_key and event.row_key.value:
            row_index = int(event.row_key.value)
            if 0 <= row_index < len(self._secret_keys):
                self.selected_secret_key = self._secret_keys[row_index]

    def update_secrets(
        self, secrets: dict[str, str], preserve_cursor: bool = False
    ) -> None:
        """Update the table with new secrets data."""
        self._secrets = secrets
        self._secret_keys = sorted(secrets.keys())

        # Remember current cursor position if preserving
        current_row = self.cursor_row if preserve_cursor else 0
        self.clear()

        # Add rows - values are either masked or actual depending on what was fetched
        for idx, key in enumerate(self._secret_keys):
            value = secrets[key]
            # Truncate long values when showing actual values
            if self._showing_values and len(value) > MAX_DISPLAY_LENGTH:
                display_value = f"{value[: MAX_DISPLAY_LENGTH - 3]}..."
            else:
                display_value = value
            self.add_row(key, display_value, key=str(idx))

        # Restore cursor position or select first row
        if self.row_count > 0:
            target_row = min(current_row, self.row_count - 1)
            self.move_cursor(row=target_row)

    def action_add_secret(self) -> None:
        """Handle the add secret action."""
        modal = AddSecretModal(deployment_id=self.deployment_id)

        def on_add(result):
            if result:
                # Refresh the secrets list, preserving visibility state
                try:
                    secrets_response = api.secrets.get_secrets(
                        self.deployment_id, show_values=self._showing_values
                    )
                    if secrets_response and secrets_response.secrets:
                        self.update_secrets(
                            secrets_response.secrets, preserve_cursor=True
                        )
                    else:
                        self.update_secrets({}, preserve_cursor=True)
                except Exception:
                    pass

        self.app.push_screen(modal, on_add)

    def action_show_secret(self) -> None:
        """Toggle visibility of all secret values in the table."""
        self._showing_values = not self._showing_values

        try:
            secrets_response = api.secrets.get_secrets(
                self.deployment_id, show_values=self._showing_values
            )
            if secrets_response and secrets_response.secrets:
                self.update_secrets(secrets_response.secrets, preserve_cursor=True)
            else:
                self.update_secrets({}, preserve_cursor=True)
        except Exception as e:
            self.app.notify(f"Failed to fetch secrets: {e}", severity="error")
            self._showing_values = not self._showing_values

    def action_edit_secret(self) -> None:
        """Handle the edit secret action."""
        if not self.selected_secret_key:
            return

        # Get the current value - if showing values, use cached, otherwise fetch
        if self._showing_values:
            current_value = self._secrets.get(self.selected_secret_key, "")
        else:
            try:
                current_value = api.secrets.get_secret_value(
                    self.deployment_id, self.selected_secret_key
                )
            except Exception:
                current_value = ""

        modal = EditSecretModal(
            deployment_id=self.deployment_id,
            secret_key=self.selected_secret_key,
            current_value=current_value,
        )

        def on_edit(result):
            if result:
                # Refresh the secrets list, preserving visibility state
                try:
                    secrets_response = api.secrets.get_secrets(
                        self.deployment_id, show_values=self._showing_values
                    )
                    if secrets_response and secrets_response.secrets:
                        self.update_secrets(
                            secrets_response.secrets, preserve_cursor=True
                        )
                    else:
                        self.update_secrets({}, preserve_cursor=True)
                except Exception:
                    pass

        self.app.push_screen(modal, on_edit)

    def action_delete_secret(self) -> None:
        """Handle the delete secret action."""
        if not self.selected_secret_key:
            return

        modal = DeleteSecretModal(
            deployment_id=self.deployment_id,
            secret_key=self.selected_secret_key,
        )

        def on_delete(result):
            if result:
                # Refresh the secrets list, preserving visibility state
                try:
                    secrets_response = api.secrets.get_secrets(
                        self.deployment_id, show_values=self._showing_values
                    )
                    if secrets_response and secrets_response.secrets:
                        self.update_secrets(
                            secrets_response.secrets, preserve_cursor=True
                        )
                    else:
                        self.update_secrets({}, preserve_cursor=True)
                except Exception:
                    pass

        self.app.push_screen(modal, on_delete)


class SecretUpdated(Message):
    """Message sent when a secret is updated."""

    def __init__(self, secret_key: str) -> None:
        super().__init__()
        self.secret_key = secret_key


class SecretDeleted(Message):
    """Message sent when a secret is deleted."""

    def __init__(self, secret_key: str) -> None:
        super().__init__()
        self.secret_key = secret_key
