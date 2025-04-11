import typer
from typing import Optional

from cli.api import api
from cli.logging import logger

app = typer.Typer(help="Scale a machine's resources")

@app.command()
def scale(
    machine_name: str = typer.Argument(..., help="Name of the machine to scale"),
    cpu: Optional[int] = typer.Option(None, help="New number of CPUs"),
    memory: Optional[int] = typer.Option(None, help="New memory in GB"),
):
    """Scale a machine's resources"""
    try:
        if not any([cpu, memory]):
            logger.error("At least one resource must be specified to scale")
            return

        result = api.machines.scale_machine(
            machine_name=machine_name,
            cpu=cpu,
            memory=memory,
        )

        if result:
            scaled_machine = api.machines.get_machines(machine_name)
            if scaled_machine:
                logger.table(scaled_machine)

    except Exception as e:
        logger.error(f"Error scaling machine: {e}")
