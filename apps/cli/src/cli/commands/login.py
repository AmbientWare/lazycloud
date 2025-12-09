import webbrowser

import httpx
import typer
from rich.console import Console
from rich.text import Text

from cli.api import api
from cli.config import config
from cli.ui.colors import Colors
from cli.ui.components.card import Card
from cli.ui.components.info_cards import ErrorCard, SuccessDetailsCard
from cli.ui.textual.theme import Icons

app = typer.Typer(help="Login to LazyCloud")
console = Console()


@app.command()
def login():
    """Login to LazyCloud via browser authentication"""
    try:
        # Fetch auth config from backend
        try:
            auth_config = api.auth.get_config()
            client_id = auth_config["workos_client_id"]
        except httpx.HTTPStatusError as e:
            error_card = ErrorCard(
                message=f"Failed to connect to LazyCloud: {e.response.status_code}",
                title="Connection Error",
                suggestion="Check your internet connection and try again.",
            )
            console.print(error_card)
            raise typer.Exit(1)
        except httpx.RequestError as e:
            error_card = ErrorCard(
                message=f"Failed to connect to LazyCloud: {e}",
                title="Connection Error",
                suggestion="Check your internet connection and try again.",
            )
            console.print(error_card)
            raise typer.Exit(1)

        # Request device authorization
        try:
            auth_data = api.auth.request_device_authorization(client_id)
        except httpx.HTTPStatusError as e:
            error_card = ErrorCard(
                message=f"Failed to initiate login: {e.response.text}",
                title="Authentication Error",
            )
            console.print(error_card)
            raise typer.Exit(1)

        device_code = auth_data["device_code"]
        user_code = auth_data["user_code"]
        verification_uri = auth_data["verification_uri"]
        verification_uri_complete = auth_data["verification_uri_complete"]
        expires_in = auth_data.get("expires_in", 300)
        interval = auth_data.get("interval", 5)

        # Show verification code first
        content = Text()
        content.append("Your code: ", style=Colors.Ansi.text_muted)
        content.append(f"{user_code}\n\n", style=f"bold {Colors.Ansi.text_white}")
        content.append(
            "If the browser doesn't open, visit:\n", style=Colors.Ansi.text_muted
        )
        content.append(f"{verification_uri}\n\n", style=Colors.Ansi.secondary)
        content.append(
            "Waiting for authentication...", style=f"italic {Colors.Ansi.text_muted}"
        )

        auth_card = Card(
            content=content,
            title=f"{Icons.LOCK_KEY} Login",
            border_style=Colors.Ansi.info,
        )
        console.print(auth_card)

        # Then try to open browser
        try:
            webbrowser.open(verification_uri_complete)
        except Exception:
            pass  # Browser opening is optional

        # Poll for tokens
        try:
            token_data = api.auth.poll_for_tokens(
                client_id, device_code, expires_in, interval
            )
        except Exception as e:
            error_card = ErrorCard(
                message=str(e),
                title="Authentication Failed",
                suggestion="Please try again with 'lazycloud login'",
            )
            console.print(error_card)
            raise typer.Exit(1)

        # Store tokens
        access_token = token_data["access_token"]
        refresh_token = token_data.get("refresh_token")
        config.set_tokens(access_token, refresh_token)

        try:
            workspaces = api.workspaces.list_workspaces()

            personal_workspace = next(
                (ws for ws in workspaces if ws.get("is_personal")), None
            )

            if not personal_workspace:
                error_card = ErrorCard(
                    message="Could not find personal workspace.\n\nPlease contact support if this issue persists.",
                    title="Configuration Error",
                )
                console.print(error_card)
                config.clear_tokens()
                raise typer.Exit(1)

            config.set_active_workspace(
                personal_workspace["id"], personal_workspace["name"]
            )

            # Get user info from token response
            user_info = token_data.get("user", {})
            user_name = user_info.get("first_name") or user_info.get("email", "User")

            success_card = SuccessDetailsCard(
                title=f"{Icons.CHECKMARK}  Login Successful",
                message=f"Welcome, {user_name}!",
                details={
                    "Active workspace": personal_workspace["name"],
                },
            )
            console.print(success_card)

        except typer.Exit:
            raise

        except Exception as e:
            config.clear_tokens()
            error_card = ErrorCard(
                message=f"Failed to configure workspace: {e}",
                title="Configuration Error",
            )
            console.print(error_card)
            raise typer.Exit(1)

    except typer.Exit:
        raise

    except Exception as e:
        error_card = ErrorCard(message=f"Login failed: {e}", title="Error")
        console.print(error_card)
        raise typer.Exit(1)
