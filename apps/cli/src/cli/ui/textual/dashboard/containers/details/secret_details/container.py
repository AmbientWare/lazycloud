from models.secrets import BasicSecret, SecretState
from textual.message import Message
from textual.widgets import DataTable

from cli.api import api
from cli.ui.textual.dashboard.containers.details.secret_details.secrets_modals import (
    AddSecretModal,
    DeleteSecretModal,
    EditSecretModal,
)


class SecretsTable(DataTable):
    """Custom DataTable for secrets that handles its own events."""

    BINDINGS = [
        ("a", "add_secret", "Add Secret"),
        ("e", "edit_secret", "Edit Secret"),
        ("d", "delete_secret", "Delete Secret"),
        ("s", "show_secret", "Show Secret"),
        ("j,down", "cursor_down", "Move down"),
        ("k,up", "cursor_up", "Move up"),
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

        self.add_columns("Secret Name", "State", "Value")

    def on_mount(self) -> None:
        """Initialize the table on mount."""
        self.show_row_labels = False
        self.border_subtitle = (
            "↑↓/jk Navigate • a: Add • s: Show All/Hide All • e: Edit • d: Delete"
        )

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Handle row highlight events."""
        if event.row_key and event.row_key.value:
            row_index = int(event.row_key.value)
            if 0 <= row_index < len(self._secret_keys):
                self.selected_secret_key = self._secret_keys[row_index]

    def update_secrets(
        self, secrets: list[BasicSecret], preserve_cursor: bool = False
    ) -> None:
        """Update the table with new secrets data."""
        # Store secrets and build lookup structures
        self._secrets = {secret.key: secret for secret in secrets}
        self._secret_keys = sorted(self._secrets.keys())

        # Remember current cursor position if preserving
        current_row = self.cursor_row if preserve_cursor else 0

        # Clear existing rows
        self.clear()

        # Add rows with actual content (no padding needed)
        for idx, key in enumerate(self._secret_keys):
            secret = self._secrets[key]
            display_value = secret.value if self._showing_values else "••••••••"

            if secret.state:
                state_display = (
                    "[yellow]○[/yellow]"
                    if secret.state == SecretState.AWAITING_DEPLOYMENT
                    else "[green]●[/green]"
                )
            else:
                state_display = "[dim]?[/dim]"

            self.add_row(key, state_display, display_value, key=str(idx))

        # Restore cursor position or select first row
        if self.row_count > 0:
            target_row = min(current_row, self.row_count - 1)
            self.move_cursor(row=target_row)

    def action_add_secret(self) -> None:
        """Handle the add secret action."""
        modal = AddSecretModal(deployment_id=self.deployment_id)

        async def on_add(result):
            if result:
                # Refresh the secrets list, preserving visibility state
                try:
                    secrets_response = await api.secrets.get_secrets(
                        self.deployment_id, show_values=self._showing_values
                    )
                    if secrets_response and secrets_response.secrets:
                        self.update_secrets(
                            secrets_response.secrets, preserve_cursor=True
                        )
                    else:
                        self.update_secrets([], preserve_cursor=True)
                except Exception:
                    pass

        self.app.push_screen(modal, on_add)

    async def action_show_secret(self) -> None:
        """Toggle visibility of all secret values in the table."""
        self._showing_values = not self._showing_values

        try:
            secrets_response = await api.secrets.get_secrets(
                self.deployment_id, show_values=self._showing_values
            )
            if secrets_response and secrets_response.secrets:
                self.update_secrets(secrets_response.secrets, preserve_cursor=True)
            else:
                self.update_secrets([], preserve_cursor=True)
        except Exception as e:
            self.app.notify(f"Failed to fetch secrets: {e}", severity="error")
            self._showing_values = not self._showing_values

    async def action_edit_secret(self) -> None:
        """Handle the edit secret action."""
        if not self.selected_secret_key:
            return

        # Get the secret object to preserve its source
        secret = self._secrets.get(self.selected_secret_key)
        if not secret:
            return

        # Get the current value - if showing values, use cached, otherwise fetch
        if self._showing_values:
            current_value = secret.value
        else:
            try:
                current_value = await api.secrets.get_secret_value(
                    self.deployment_id, self.selected_secret_key
                )
            except Exception:
                current_value = ""

        modal = EditSecretModal(
            deployment_id=self.deployment_id,
            secret_key=self.selected_secret_key,
            current_value=current_value,
            source=secret.source,
        )

        async def on_edit(result):
            if result:
                # Refresh the secrets list, preserving visibility state
                try:
                    secrets_response = await api.secrets.get_secrets(
                        self.deployment_id, show_values=self._showing_values
                    )
                    if secrets_response and secrets_response.secrets:
                        self.update_secrets(
                            secrets_response.secrets, preserve_cursor=True
                        )
                    else:
                        self.update_secrets([], preserve_cursor=True)
                except Exception:
                    pass

        self.app.push_screen(modal, on_edit)

    def action_delete_secret(self) -> None:
        """Handle the delete secret action."""
        if not self.selected_secret_key:
            return

        # Get the secret object to preserve its source
        secret = self._secrets.get(self.selected_secret_key)
        if not secret:
            return

        modal = DeleteSecretModal(
            deployment_id=self.deployment_id,
            secret_key=self.selected_secret_key,
            source=secret.source,
        )

        async def on_delete(result):
            if result:
                # Refresh the secrets list, preserving visibility state
                try:
                    secrets_response = await api.secrets.get_secrets(
                        self.deployment_id, show_values=self._showing_values
                    )
                    if secrets_response and secrets_response.secrets:
                        self.update_secrets(
                            secrets_response.secrets, preserve_cursor=True
                        )
                    else:
                        self.update_secrets([], preserve_cursor=True)
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
