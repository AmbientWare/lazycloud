import typer
from rich.console import Console

from lazycloud_cli.config import config
from lazycloud_cli.ui.views import AuthView

app = typer.Typer(help="List API keys")
console = Console()
view = AuthView(console)


@app.command()
def list():
    """List all API keys"""
    try:
        keys = config.list_api_keys()
        active_key = config.active_api_key

        if not keys:
            view.show_no_keys()
            return

        view.show_api_keys(keys, active_key)

    except Exception as e:
        view.show_error(f"Failed to list API keys: {e}")
        raise typer.Exit(1)
