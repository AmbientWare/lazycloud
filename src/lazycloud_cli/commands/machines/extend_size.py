import typer
from rich.console import Console

from lazycloud_cli.api import api
from lazycloud_cli.logging import logger

app = typer.Typer(help="Extend the size of a machine")
console = Console()


@app.command()
def extend_size(
    name: str = typer.Argument(..., help="Name of the machine to extend the size of"),
):
    """Extend the size of a machine"""
    try:
        # Get machine details from API
        m_list = api.machines.get_machines(name)
        if not m_list:
            logger.error(f"Machine '{name}' not found")
            raise typer.Exit(1)

        machine = m_list[0]

    except Exception as e:
        logger.error(f"Failed to get machine details: {e}")
        raise typer.Exit(1)

    # prompt user for new size
    new_size = typer.prompt(
        f"Enter the new size for {name} (current size: {machine['disk_size']}GB)",
        type=int,
    )

    cases = {
        (
            new_size <= machine["disk_size"]
        ): "New size must be greater than current size",
        (new_size > 500): "New size must be less than or equal to 500GB",
    }

    for case, error_message in cases.items():
        if case:
            logger.error(error_message)
            raise typer.Exit(1)

    # make sure new size is greater than current size
    if new_size <= machine["disk_size"]:
        logger.error("New size must be greater than current size")
        raise typer.Exit(1)
    # make sure new size is <= 500GB
    if new_size > 500:
        logger.error("New size must be less than or equal to 500GB")
        raise typer.Exit(1)

    # confirm the extension since volumes cannot be shrunk
    typer.confirm(
        "Are you sure you want to extend the volume? Volumes cannot be shrunk.",
        abort=True,
    )

    # request volume ap to extend size for machine
    try:
        api.volumes.extend_volume(machine_id=machine["id"], size=new_size)
    except Exception as e:
        logger.error(f"Failed to extend volume: {e}")
        raise typer.Exit(1)

    logger.success(f"Volume for {name} extended to {new_size}GB")
