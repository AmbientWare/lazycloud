import typer
from rich.console import Console

from lazycloud_cli.api import api
from lazycloud_cli.logging import logger

app = typer.Typer(help="Get machine details")
console = Console()


@app.command()
def get(
    name: str = typer.Argument(..., help="Name of the machine to get details for"),
):
    """Get detailed information about a specific machine"""
    try:
        # Get machine details from API
        machines = api.machines.get_machines(name)
        if not machines:
            logger.error(f"Machine '{name}' not found")
            raise typer.Exit(1)

        # Display the table
        logger.table(machines)

    except Exception as e:
        logger.error(f"Failed to get machine details: {e}")
        raise typer.Exit(1)
