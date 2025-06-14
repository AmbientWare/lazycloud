import typer
from rich.console import Console
import click

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
        type=click.IntRange(min=machine["disk_size"], max=500),
    )

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
