import typer
from rich.console import Console

from lazycloud_cli.config import config
from lazycloud_cli.ui.views import AuthView

app = typer.Typer(help="Set the active API key")
console = Console()
view = AuthView(console)


@app.command()
def set(
    name: str = typer.Argument(..., help="Name of the API key to set as active"),
):
    """Set the active API key"""
    try:
        # Check if key exists
        if name not in config.list_api_keys():
            view.show_key_not_found(name)
            raise typer.Exit(1)

        # Set the active key
        config.active_api_key = name
        view.show_active_key_set(name)

    except typer.Exit:
        raise

    except Exception as e:
        view.show_error(f"Failed to set active API key: {e}")
        raise typer.Exit(1)
