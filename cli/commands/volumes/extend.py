import typer
from cli.api import api
from cli.logging import logger

app = typer.Typer(help="Extend machine volumes")

@app.command()
def extend(
    machine_name: str = typer.Argument(..., help="Name of the machine to extend volume for"),
    size: int = typer.Argument(..., help="New size of the volume in GB"),
):
    """Extend a machine's storage volume to a specific size in GB"""
    try:
        # Verify machine exists
        machines = api.machines.get_machines(machine_name)
        if not machines:
            logger.error(f"Machine '{machine_name}' not found")
            raise typer.Exit(1)

        # Extend the volume
        api.machines.extend_volume(machine_name, size)
        logger.success(f"Volume for machine '{machine_name}' extended to {size}GB")

    except Exception as e:
        logger.error(f"Failed to extend volume: {e}")
        raise typer.Exit(1)
