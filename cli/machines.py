import click
from typing import Optional
import subprocess

from cli import machine
from cli.utils import (
    make_table_view,
    get_default_key_path,
    add_to_ssh_config,
    remove_from_ssh_config,
)
from cli.api import MachineAPI
from cli.tokens import tokens  # Import the tokens group


@machine.command()
@click.argument("name")
@click.option("--public-key", help="Path to SSH public key file")
@click.option("--region", help="Region to deploy the machine")
@click.option("--cpu-kind", help="CPU kind")
@click.option("--cpu", type=int, help="Number of CPUs")
@click.option("--memory", type=float, help="Memory in GB")
@click.option("--volume-size", type=int, help="Volume size in GB")
def create(
    name: str,
    public_key: Optional[str],
    region: Optional[str],
    cpu_kind: Optional[str],
    cpu: Optional[int],
    memory: Optional[int],
    volume_size: Optional[int],
):
    """Create a new machine"""
    try:
        # Prompt for name if not provided
        if not name:
            name = click.prompt("Enter machine name")

        # Get default key path if not provided
        if not public_key:
            default_path = get_default_key_path()
            public_key = click.prompt(
                "Enter path to SSH public key file. Enter to use default: ",
                default=default_path,
                show_default=True,
            )

        # Read the public key file
        try:
            if public_key is None:
                raise ValueError("Public key path cannot be None")
            public_key_content = MachineAPI.read_public_key(public_key)

        except FileNotFoundError as e:
            click.echo(str(e), err=True)
            return

        except Exception as e:
            click.echo(f"Error reading public key file: {e}", err=True)
            return

        # Create machine using API
        try:
            if name is None:
                raise ValueError("Machine name cannot be None")

            api = MachineAPI()
            result = api.create_machine(
                name=name,
                public_key=public_key_content,
                region=region,
                cpu_kind=cpu_kind,
                cpu=cpu,
                memory=memory,
                volume_size=volume_size,
            )
            headers = list(result.keys())
            click.echo(make_table_view([result], headers))

        except Exception as e:
            click.echo(f"Error creating machine: {e}", err=True)
            return

        # prompt to add to ssh config
        should_add_to_ssh_config = click.confirm(
            "Would you like to add this machine to your SSH config?", default=True
        )
        if should_add_to_ssh_config:
            # add to ssh config
            alias, port = api.get_machine_alias(name)
            if alias is None or port is None:
                click.echo(
                    "Error getting machine alias. Please try again by running `machines connect add <machine-name>`.",
                    err=True,
                )
                return

            user_id = api.get_user_id()
            add_to_ssh_config(name, alias, port, user_id)

    except Exception as e:
        click.echo(f"Error creating machine: {e}", err=True)


@machine.command()
@click.argument("machine-name")
@click.option("--cpu-kind", help="CPU kind")
@click.option("--cpu", type=int, help="Number of CPUs")
@click.option("--memory", type=float, help="Memory in GB")
@click.option("--region", help="Region to deploy the machine")
def scale(
    machine_name: str,
    cpu_kind: Optional[str],
    cpu: Optional[int],
    memory: Optional[int],
    region: Optional[str],
):
    """Scale a machine"""
    try:
        api = MachineAPI()
        api.scale_machine(machine_name, cpu_kind, cpu, memory, region)
        click.echo(f"Successfully scaled machine {machine_name}")
    except Exception as e:
        click.echo(f"Error scaling machine: {e}", err=True)


@machine.command()
@click.argument("machine-name")
def destroy(machine_name: str):
    """Destroy a machine"""
    try:
        # prompt for confirmation
        should_destroy = click.confirm(
            "Are you sure you want to destroy this machine?", default=False
        )
        if not should_destroy:
            click.echo("Machine not destroyed")
            return

        click.echo(f"Destroying machine {machine_name}...")
        api = MachineAPI()
        api.destroy_machine(machine_name)

        # remove from ssh config if it exists
        remove_from_ssh_config(machine_name)

        click.echo(f"Successfully destroyed machine {machine_name}")
    except Exception as e:
        click.echo(f"Error deleting machine: {e}", err=True)


@machine.command()
@click.argument("machine-name", required=False)
def get(machine_name: Optional[str]):
    """Get machine(s). If machine-name is provided, get that specific machine."""
    try:
        api = MachineAPI()
        machines = api.get_machines(machine_name)

        # Define headers based on the first machine's keys
        if machines:
            headers = list(machines[0].keys())
            click.echo(make_table_view(machines, headers))
        else:
            click.echo("No machines found")
    except Exception as e:
        click.echo(f"Error getting machine(s): {e}", err=True)


@machine.command(name="ls")
def list_machines():
    """List all machines"""
    try:
        api = MachineAPI()
        machines = api.get_machines()
        if machines:
            headers = list(machines[0].keys())
            click.echo(make_table_view(machines, headers))
        else:
            click.echo("No machines found")

    except Exception as e:
        click.echo(f"Error listing machines: {e}", err=True)


@machine.command(name="connect")
@click.argument("machine-name")
@click.option("--ssh-key", help="Path to SSH private key file")
def connect_machine(machine_name: str, ssh_key: Optional[str]):
    """Connect to a machine"""
    try:
        api = MachineAPI()
        alias, port = api.get_machine_alias(machine_name)
        if alias:
            if not ssh_key:
                ssh_key = get_default_key_path().replace(
                    ".pub", ""
                )  # Remove .pub extension to get private key

            click.echo(f"Connecting to machine {machine_name}...")
            api = MachineAPI()
            user_id = api.get_user_id()
            ssh_command = [
                "ssh",
                "-i",
                ssh_key,
                f"{user_id}@{alias}",
                "-p",
                str(port),
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "ForwardAgent=yes",
                "-o",
                "ConnectTimeout=30",
            ]
            subprocess.run(ssh_command)
        else:
            click.echo("No machines found")

    except Exception as e:
        click.echo(f"Error connecting to machine: {e}", err=True)


@machine.command(name="ssh-add")
@click.argument("machine-name")
def ssh_add(machine_name: str):
    """Add a machine to SSH config"""
    try:
        api = MachineAPI()
        alias, port = api.get_machine_alias(machine_name)
        if alias is None or port is None:
            click.echo(
                "Error getting machine alias. Please make sure the machine exists.",
                err=True,
            )
            return

        user_id = api.get_user_id()
        add_to_ssh_config(machine_name, alias, port, user_id)
        click.echo(f"Successfully added machine {machine_name} to SSH config")

    except Exception as e:
        click.echo(f"Error adding machine to SSH config: {e}", err=True)


# Add the tokens group to the main CLI
machine.add_command(tokens)
