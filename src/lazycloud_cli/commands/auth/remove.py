import typer
from rich.console import Console

from lazycloud_cli.config import config
from lazycloud_cli.ui.views import AuthView

app = typer.Typer(help="Remove an API key")
console = Console()
view = AuthView(console)


@app.command()
def remove(
    name: str = typer.Argument(..., help="Name of the API key to remove"),
):
    """Remove an API key"""
    try:
        # Check if key exists
        if name not in config.list_api_keys():
            view.show_key_not_found(name)
            raise typer.Exit(1)

        if not view.confirm_remove_key(name):
            return

        # Remove the key
        config.remove_api_key(name)
        view.show_key_removed(name)

    except typer.Exit:
        raise

    except Exception as e:
        view.show_error(f"Failed to remove API key: {e}")
        raise typer.Exit(1)
