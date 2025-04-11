import typer
from cli.config import config
from cli.logging import logger

app = typer.Typer(help="Add a new API key")


@app.command()
def add(
    name: str = typer.Argument(..., help="Name for the API key"),
    value: str = typer.Option(..., prompt=True, hide_input=True, help="API key value"),
):
    """Add a new API key"""
    try:
        # Check if key name already exists
        if name in config.list_api_keys():
            if not typer.confirm(f"API key '{name}' already exists. Overwrite?"):
                return

        # Add the API key
        config.add_api_key(name, value)
        logger.success(f"Added API key '{name}'")

        # If this is the first key, set it as active
        if not config.active_api_key:
            config.active_api_key = name
            logger.info(f"Set '{name}' as active API key")

    except Exception as e:
        logger.error(f"Failed to add API key: {e}")
        raise typer.Exit(1)
