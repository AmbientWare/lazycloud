import typer
from cli.config import config
from cli.logging import logger

app = typer.Typer(help="Set the active API key")


@app.command()
def set_active(
    name: str = typer.Argument(..., help="Name of the API key to set as active"),
):
    """Set the active API key"""
    try:
        # Check if key exists
        if name not in config.list_api_keys():
            logger.error(f"API key '{name}' not found")
            raise typer.Exit(1)

        # Set the active key
        config.active_api_key = name
        logger.success(f"Set '{name}' as active API key")

    except Exception as e:
        logger.error(f"Failed to set active API key: {e}")
        raise typer.Exit(1)
