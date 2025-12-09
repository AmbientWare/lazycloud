import typer
from rich.console import Console

from cli.config import config
from cli.ui.components.info_cards import ErrorCard, SuccessCard

app = typer.Typer(help="Logout from LazyCloud")
console = Console()


@app.command()
def logout():
    """Logout from LazyCloud and clear stored credentials"""
    try:
        if not config.access_token:
            error_card = ErrorCard(
                message="You are not currently logged in.",
                title="Not Logged In",
            )
            console.print(error_card)
            raise typer.Exit(1)

        # Clear tokens and workspace
        config.clear_tokens()
        config.clear_active_workspace()

        success_card = SuccessCard(
            message="Successfully logged out. Run 'lazycloud login' to authenticate again.",
            title="Logged Out",
        )
        console.print(success_card)

    except typer.Exit:
        raise

    except Exception as e:
        error_card = ErrorCard(message=f"Logout failed: {e}", title="Error")
        console.print(error_card)
        raise typer.Exit(1)
