import typer
from rich.console import Console
from rich.prompt import Prompt
from rich.text import Text

from cli.api import api
from cli.config import config
from cli.ui.colors import Colors
from cli.ui.components.card import Card
from cli.ui.components.info_cards import (
    ActionProgressCard,
    ErrorCard,
    SuccessDetailsCard,
)

app = typer.Typer(help="Login with API key")
console = Console()


@app.command()
def login(
    api_key: str = typer.Argument(
        None,
        help="API key to use for authentication (will prompt if not provided)",
    ),
):
    """Login with your LazyCloud API key"""
    try:
        # If no API key provided, prompt for it
        if not api_key:
            # Show informational card
            info_card = Card(
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
            console.print(info_card)

            # Prompt for API key
            api_key = Prompt.ask(
                Text("API Key", style=f"bold {Colors.Ansi.primary}"),
                password=True,
                show_default=False,
            )
            console.print()

        if not api_key or not api_key.strip():
            error_card = ErrorCard(
                message="API key cannot be empty", title="🔑 Invalid Input"
            )
            console.print(error_card)
            raise typer.Exit(1)

        # Validate the API key
        validating_card = ActionProgressCard(action="Validating API key", icon="🔍")
        console.print(validating_card)

        # Store the API key temporarily to test it
        config.set_api_key(api_key.strip())

        try:
            # Test the API key by fetching workspaces
            workspaces = api.workspaces.list_workspaces()

            # Find the personal workspace
            personal_workspace = next(
                (ws for ws in workspaces if ws.get("is_personal")), None
            )

            if not personal_workspace:
                error_card = ErrorCard(
                    message="Could not find personal workspace.\n\nPlease contact support if this issue persists.",
                    title="🔑 Configuration Error",
                )
                console.print(error_card)
                # Clean up the key before exiting
                config.clear_api_key()
                raise typer.Exit(1)

            # Set the personal workspace as active
            config.set_active_workspace(
                personal_workspace["id"], personal_workspace["name"]
            )

            # Show success
            success_card = SuccessDetailsCard(
                title="🔑 Login Successful",
                message="Successfully logged in!",
                details={
                    "Active workspace": personal_workspace["name"],
                    "Role": personal_workspace.get("role", "unknown"),
                },
            )
            console.print(success_card)

        except typer.Exit:
            # Re-raise typer.Exit to allow it to propagate
            raise
        except Exception as e:
            # If validation fails, remove the key
            config.clear_api_key()

            error_card = ErrorCard(
                message=f"The API key is invalid or could not connect to the server.\n\nError: {e}",
                title="🔑 Invalid API Key",
                suggestion="Please check the key and try again.",
            )
            console.print(error_card)
            raise typer.Exit(1)

    except typer.Exit:
        raise
    except Exception as e:
        error_card = ErrorCard(message=f"Login failed: {e}", title="🔑 Error")
        console.print(error_card)
        raise typer.Exit(1)
