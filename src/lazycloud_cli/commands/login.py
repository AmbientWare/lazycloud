import typer
from rich.console import Console
from rich.prompt import Prompt
from rich.text import Text

from lazycloud_cli.api import api
from lazycloud_cli.config import config
from lazycloud_cli.ui.colors import Colors
from lazycloud_cli.ui.components.card import Card

app = typer.Typer(help="Login with API key")
console = Console()


@app.command()
def login():
    """Login with your LazyCloud API key"""
    try:
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

        if not api_key or not api_key.strip():
            error_card = Card(
                content=Text("API key cannot be empty", style=Colors.Ansi.error),
                title="🔑 Invalid Input",
                border_style=Colors.Ansi.error,
            )
            console.print(error_card)
            raise typer.Exit(1)

        # Validate the API key
        validating_card = Card(
            content=Text("🔍 Validating API key...", style=Colors.Ansi.info),
            border_style=Colors.Ansi.info,
        )
        console.print(validating_card)

        # Store the API key temporarily to test it
        config.add_api_key("default", api_key.strip())

        try:
            # Test the API key by fetching workspaces
            workspaces = api.workspaces.list_workspaces()

            # Find the personal workspace
            personal_workspace = None
            for ws in workspaces:
                if ws.get("is_personal"):
                    personal_workspace = ws
                    break

            if not personal_workspace:
                error_card = Card(
                    content=Text(
                        "Could not find personal workspace.\n\n"
                        "Please contact support if this issue persists.",
                        style=Colors.Ansi.error,
                    ),
                    title="🔑 Configuration Error",
                    border_style=Colors.Ansi.error,
                )
                console.print(error_card)
                # Clean up the key before exiting
                config.remove_api_key("default")
                raise typer.Exit(1)

            # Set the personal workspace as active
            config.set_active_workspace(
                personal_workspace["id"], personal_workspace["name"]
            )

            # Show success
            success_content = Text()
            success_content.append(
                "✓ Successfully logged in!\n\n", style=Colors.Ansi.success
            )
            success_content.append(
                f"Active workspace: {personal_workspace['name']}\n",
                style=Colors.Ansi.text_muted,
            )
            success_content.append(
                f"Role: {personal_workspace.get('role', 'unknown')}",
                style=Colors.Ansi.text_muted,
            )

            success_card = Card(
                content=success_content,
                title="🔑 Login Successful",
                border_style=Colors.Ansi.success,
            )
            console.print(success_card)

        except typer.Exit:
            # Re-raise typer.Exit to allow it to propagate
            raise
        except Exception as e:
            # If validation fails, remove the key
            config.remove_api_key("default")

            error_card = Card(
                content=Text(
                    f"The API key is invalid or could not connect to the server.\n\n"
                    f"Error: {e}\n\n"
                    "Please check the key and try again.",
                    style=Colors.Ansi.error,
                ),
                title="🔑 Invalid API Key",
                border_style=Colors.Ansi.error,
            )
            console.print(error_card)
            raise typer.Exit(1)

    except typer.Exit:
        raise
    except Exception as e:
        error_card = Card(
            content=Text(f"Login failed: {e}", style=Colors.Ansi.error),
            title="🔑 Error",
            border_style=Colors.Ansi.error,
        )
        console.print(error_card)
        raise typer.Exit(1)
