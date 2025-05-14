import typer

from lazycloud_cli.api import api
from lazycloud_cli.logging import logger
from lazycloud_cli.ssh_config import ssh_config_manager

app = typer.Typer(help="Delete a machine")


@app.command()
def destroy(
    name: str = typer.Argument(..., help="Name of the machine to delete"),
):
    """Delete a machine"""
    logger.confirm(f"Are you sure you want to delete machine {name}?")

    try:
        result = api.machines.delete_machine(name)
        if result:
            logger.success(f"Successfully destroyed machine {name}")
            # remove the machine from the ssh config
            ssh_config_manager.remove_machine(name)

        else:
            logger.error(f"Machine {name} not found")

    except Exception as e:
        logger.error(f"Error deleting machine: {e}")
