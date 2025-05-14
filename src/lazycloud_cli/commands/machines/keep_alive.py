import typer

from lazycloud_cli.api import api
from lazycloud_cli.logging import logger

app = typer.Typer(help="List machines")


@app.command()
def keep_alive(name: str):
    """Keep a machine alive"""
    logger.confirm(
        f"This action will cause '{name}' to restart. Are you sure you want to continue?"
    )
    try:
        api.machines.auto_stop(name, enabled=False)
        logger.info(f"Machine '{name}' will be kept alive")

    except Exception as e:
        logger.error(f"Failed to enable keep alive for machine '{name}': {e}")
        raise typer.Exit(1)
