import click
import sys

from cli import machine
from cli.api import MachineAPI


@machine.group(context_settings={"help_option_names": ["-h", "--help"]})
def volume():
    """Volume management commands"""
    pass


@volume.command()
@click.argument("machine_name")
@click.argument("volume_size")
def extend(machine_name: str, volume_size: int):
    """Extend the volume of a machine"""
    try:
        api = MachineAPI()
        api.extend_volume(machine_name, volume_size)
        click.echo(f"Volume {machine_name} extended to {volume_size}GB")

    except Exception as e:
        click.echo(str(e), err=True)
        sys.exit(1)
