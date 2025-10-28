from textual.app import ComposeResult
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Static

from lazycloud_cli.api import api
from lazycloud_cli.ui.textual.components import Container
from lazycloud_cli.ui.textual.dashboard.containers.details.container import (
    ContentContainer,
    DisplayMode,
)
from lazycloud_cli.ui.textual.dashboard.containers.details.secret_details import (
    SecretDeleted,
    SecretUpdated,
)
from lazycloud_cli.ui.textual.theme import Icons


class SecretsContainer(Container):
    # Reactive attributes
    deployment_id: reactive[str | None] = reactive(None)
    selected_secret_key: reactive[str | None] = reactive(None)
    secret_count: reactive[int] = reactive(0)

    BINDINGS = []

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.border_title = f"{Icons.LOCK_KEY} [3] Secrets"

    def compose(self) -> ComposeResult:
        """Create the secrets count display"""
        yield Static("", id="secrets-count")

    def on_mount(self) -> None:
        """Style the container when mounted."""
        self.can_focus = True
        if self.deployment_id:
            self._update_count(self.deployment_id)

    def watch_deployment_id(self, old_id: str | None, new_id: str | None) -> None:
        """Update secret count when deployment changes."""
        if new_id:
            self._update_count(new_id)
        else:
            self.secret_count = 0

    def watch_secret_count(self, old_count: int, new_count: int) -> None:
        """Update the display when secret count changes."""
        try:
            widget = self.query_one("#secrets-count", Static)
            text = f"{new_count} secret" if new_count == 1 else f"{new_count} secrets"
            widget.update(f"[bold]{text}[/bold]")
        except Exception:
            pass

    def _update_count(self, deployment_id: str) -> None:
        """Fetch and update the secret count."""
        try:
            secrets = api.secrets.get_secrets(deployment_id)
            self.secret_count = (
                len(secrets.secrets) if secrets and secrets.secrets else 0
            )
        except Exception:
            self.secret_count = 0

    async def on_container_updated(self, message: Message) -> None:
        """Handle container updated messages from secret details."""
        if isinstance(message, (SecretUpdated, SecretDeleted)):
            # Refresh the count when secrets are updated/deleted
            if self.deployment_id:
                self._update_count(self.deployment_id)
            message.stop()

    def on_click(self) -> None:
        """Handle mouse clicks - switch to secrets view."""
        self.app.action_switch_to_secrets()

    def watch_selected_secret_key(self, _old_value, new_value) -> None:
        """React when a secret is selected - switch to secrets view (shows all secrets)"""
        self.log(
            f"SecretsContainer.watch_selected_secret_key: old={_old_value}, new={new_value}"
        )
        if new_value:
            content = self.app.query_one(ContentContainer)
            self.log(
                "SecretsContainer: Setting content.display_mode=DisplayMode.SECRET"
            )
            content.display_mode = DisplayMode.SECRET
            self.log("SecretsContainer: Done updating ContentContainer")
