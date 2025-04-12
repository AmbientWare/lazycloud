import typer

from cli.api import api
from cli.logging import logger
from cli.ssh_config import ssh_config_manager

app = typer.Typer(help="Delete a machine")


@app.command()
def destroy(
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
            # remove the machine from the ssh config
            ssh_config_manager.remove_machine(machine_name)

        else:
            logger.error(f"Machine {machine_name} not found")

    except Exception as e:
        logger.error(f"Error deleting machine: {e}")
