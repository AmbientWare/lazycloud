import typer

from cli.api import api
from cli.logging import logger

app = typer.Typer(help="Delete a machine")


@app.command()
def rm(
    machine_name: str = typer.Argument(..., help="Name of the machine to delete"),
    force: bool = typer.Option(
        False, "--force", "-f", help="Force deletion without confirmation"
    ),
):
    """Delete a machine"""
    try:
        if not force:
            confirm = typer.confirm(
                f"Are you sure you want to delete machine {machine_name}?"
            )
            if not confirm:
                return

        result = api.machines.delete_machine(machine_name)
        if result:
            logger.info(f"Successfully deleted machine {machine_name}")

    except Exception as e:
        logger.error(f"Error deleting machine: {e}")
