import typer

from lazycloud_cli.config import config
from lazycloud_cli.logging import logger

app = typer.Typer(help="Remove an API key")


@app.command()
def rm(
    name: str = typer.Argument(..., help="Name of the API key to remove"),
):
    """Remove an API key"""
    try:
        # Check if key exists
        if name not in config.list_api_keys():
            logger.error(f"API key '{name}' not found")
            raise typer.Exit(1)

        logger.confirm(f"Are you sure you want to remove API key '{name}'?")

        # Remove the key
        config.remove_api_key(name)
        logger.success(f"Removed API key '{name}'")

    except Exception as e:
        logger.error(f"Failed to remove API key: {e}")
        raise typer.Exit(1)
