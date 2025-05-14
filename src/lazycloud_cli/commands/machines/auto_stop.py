import typer

from lazycloud_cli.api import api
from lazycloud_cli.logging import logger

app = typer.Typer(help="List machines")


@app.command()
def auto_stop(name: str):
    """Auto stop a machine"""
    logger.confirm(
        f"This action will cause '{name}' to restart. Are you sure you want to continue?"
    )
    try:
        api.machines.auto_stop(name, enabled=True)
        logger.info(f"Machine '{name}' will be auto stopped")

    except Exception as e:
        logger.error(f"Failed to enable auto stop for machine '{name}': {e}")
        raise typer.Exit(1)
