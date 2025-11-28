from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from cli.ui.colors import Colors
from cli.ui.components.card import Card
from cli.ui.components.confirmation import (
    DestructiveConfirmationDialog,
    SimpleConfirmationDialog,
)


class ApiKeyListCard(Card):
    """Card for displaying API keys."""

    def __init__(self, keys: dict[str, str], active_key: str | None = None):
        """Initialize API key list card."""
        if not keys:
            content = Text("No API keys configured", style=Colors.Ansi.text_muted)
        else:
            table = Table(
                show_header=True, header_style=f"bold {Colors.Ansi.primary}", box=None
            )
            table.add_column("Name", style=Colors.Ansi.primary)
            table.add_column("Key", style=Colors.Ansi.text_muted)
            table.add_column("Status", style=Colors.Ansi.success)

            for name, value in keys.items():
                # Mask the key value for security
                masked_value = (
                    value[:8] + "..." + value[-4:] if len(value) > 12 else "****"
                )
                status = "✓ Active" if name == active_key else ""
                table.add_row(name, masked_value, status)

            content = table

        super().__init__(
            content=content,
            title="🔑 API Keys",
            subtitle=f"{len(keys)} keys configured" if keys else None,
            border_style=Colors.Ansi.primary,
        )


class ApiKeyDetailsCard(Card):
    """Card for showing API key details."""

    def __init__(self, name: str, value: str, is_active: bool = False):
        """Initialize API key details card."""
        table = Table(show_header=False, box=None)
        table.add_column("Property", style=Colors.Ansi.primary)
        table.add_column("Value")

        table.add_row("🔑 Name", name)
        table.add_row("📝 Value", value[:12] + "..." if len(value) > 12 else value)

        if is_active:
            table.add_row("✅ Status", Text("Active", style=Colors.Ansi.success))

        super().__init__(
            content=table,
            title="API Key Details",
            border_style=Colors.Ansi.success if is_active else Colors.Ansi.primary,
        )


class AuthView:
    """Main view orchestrator for auth commands."""

    def __init__(self, console: Console):
        """Initialize the auth view."""
        self.console = console

    def show_api_keys(self, keys: dict[str, str], active_key: str | None = None):
        """Display list of API keys."""
        self.console.print(ApiKeyListCard(keys, active_key))

    def show_no_keys(self):
        """Show message when no API keys exist."""
        card = Card(
            content=Text(
                "No API keys configured.\n\n"
                "Add your first API key with:\n"
                "  lazycloud auth add <name>",
                style=Colors.Ansi.text_muted,
            ),
            title="🔑 No API Keys",
            border_style=Colors.Ansi.warning,
        )
        self.console.print(card)

    def confirm_overwrite(self, name: str) -> bool:
        """Confirm overwriting an existing API key."""
        dialog = SimpleConfirmationDialog(
            action=f"overwrite API key '{name}'",
            details=[
                f"API key '{name}' already exists",
                "The existing key will be replaced",
                "This action cannot be undone",
            ],
            title="Existing Key Found",
        )
        dialog.default = False
        return dialog.show(self.console)

    def prompt_api_key(self) -> str:
        """Prompt for API key value."""
        # Show informational card before prompting
        card = Card(
            content=Text(
                "Please enter your LazyCloud API key.\n\n"
                "You can find your API key at:\n"
                "  • https://lazycloud.dev/settings/api-keys\n\n"
                "The key will be hidden as you type for security.",
                style=Colors.Ansi.text_muted,
            ),
            title="🔑 API Key Required",
            border_style=Colors.Ansi.info,
        )
        self.console.print(card)

        # Now prompt with a clearer message
        return Prompt.ask(
            Text("API Key", style=f"bold {Colors.Ansi.primary}"),
            password=True,
            show_default=False,
        )

    def show_validating_key(self):
        """Show that we're validating the API key."""
        card = Card(
            content=Text("🔍 Validating API key...", style=Colors.Ansi.info),
            border_style=Colors.Ansi.info,
        )
        self.console.print(card)

    def show_key_invalid(self):
        """Show API key validation failed."""
        card = Card(
            content=Text(
                "The API key is invalid.\n\nPlease check the key and try again.",
                style=Colors.Ansi.error,
            ),
            title="🔑 Invalid API Key",
            border_style=Colors.Ansi.error,
        )
        self.console.print(card)

    def show_key_added(self, name: str, set_active: bool = False):
        """Show successful key addition."""
        content = Text()
        content.append(
            f"Successfully added API key '{name}'\n", style=Colors.Ansi.success
        )

        if set_active:
            content.append(
                f"\nSet '{name}' as the active API key", style=Colors.Ansi.info
            )

        card = Card(
            content=content,
            title="🔑 API Key Added",
            border_style=Colors.Ansi.success,
        )
        self.console.print(card)

    def show_key_not_found(self, name: str):
        """Show API key not found error."""
        card = Card(
            content=Text(f"API key '{name}' not found", style=Colors.Ansi.error),
            title="🔑 Key Not Found",
            border_style=Colors.Ansi.error,
        )
        self.console.print(card)

    def show_active_key_set(self, name: str):
        """Show active key has been set."""
        card = Card(
            content=Text(
                f"Set '{name}' as the active API key\n\n"
                "All API requests will now use this key.",
                style=Colors.Ansi.success,
            ),
            title="🔑 Active Key Updated",
            border_style=Colors.Ansi.success,
        )
        self.console.print(card)

    def confirm_remove_key(self, name: str) -> bool:
        """Confirm API key removal."""
        dialog = DestructiveConfirmationDialog(
            resource_type="API key",
            resource_name=name,
            consequences=[
                "The API key will be permanently removed",
                "You will need to re-enter it to use it again",
            ],
        )
        return dialog.show(self.console)

    def show_key_removed(self, name: str):
        """Show successful key removal."""
        card = Card(
            content=Text(f"Removed API key '{name}'", style=Colors.Ansi.success),
            title="🔑 API Key Removed",
            border_style=Colors.Ansi.success,
        )
        self.console.print(card)

    def show_error(self, message: str):
        """Show general error message."""
        card = Card(
            content=Text(f"{message}", style=Colors.Ansi.error),
            title="🔑 Error",
            border_style=Colors.Ansi.error,
        )
        self.console.print(card)

    def show_info(self, message: str):
        """Show informational message."""
        self.console.print(Text(f"{message}", style=Colors.Ansi.info))

    def show_success(self, message: str):
        """Show success message."""
        card = Card(
            content=Text(f"{message}", style=Colors.Ansi.success),
            border_style=Colors.Ansi.success,
        )
        self.console.print(card)
