import typer

from lazycloud_cli.api import api
from lazycloud_cli.logging import logger

app = typer.Typer(help="List machines")


@app.command()
def restart(name: str):
    """Restart a machine"""
    logger.confirm(f"Are you sure you want to restart machine '{name}'?")

    try:
        api.machines.restart(name)
        logger.info(f"Machine '{name}' restarted successfully")

    except Exception as e:
        logger.error(f"Failed to restart machine '{name}': {e}")
        raise typer.Exit(1)
