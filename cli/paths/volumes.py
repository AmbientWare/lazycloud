import click
import sys

from cli.paths.machines import machines
from cli.api import machines_api
from cli.logging import logger


@machines.group()
def volume():
    """Volume management commands"""
    pass


@volume.command()
@click.argument("machine_name")
@click.argument("size", type=int)
def extend(machine_name: str, size: int):
    """Extend a machine's storage volume to a specific size in GB"""
    try:
        machines_api.extend_volume(machine_name, size)
        logger.success(f"Volume {machine_name} extended to {size}GB")

    except Exception as e:
        logger.error(str(e))
        sys.exit(1)
