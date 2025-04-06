import click
from typing import Optional
import subprocess

from cli.paths.utils import (
    make_table_view,
    get_default_key_path,
)
from cli.api import machines_api
from cli.paths.remachina import remach
from cli.logging import logger
from cli.paths.ssh import ssh_config_manager


@remach.group()
def machines():
    """Machine management commands"""
    pass


@machines.command()
@click.argument("machine_name")
@click.option("--region", help="Region to deploy the machine")
@click.option("--cpu-kind", help="CPU kind")
@click.option("--cpu", type=int, help="Number of CPUs")
@click.option("--memory", type=float, help="Memory in GB")
@click.option("--volume-size", type=int, help="Volume size in GB")
def create(
    machine_name: str,
    region: Optional[str],
    cpu_kind: Optional[str],
    cpu: Optional[int],
    memory: Optional[int],
    volume_size: Optional[int],
):
    """Create a new machine"""
    try:
        ssh_keys = machines_api.get_ssh_keys()
        if not ssh_keys:
            logger.error(
                "No SSH keys found. Please create an SSH key first with `remach ssh keys add`"
            )
            return

        # Prompt for name if not provided
        if not machine_name:
            machine_name = click.prompt("Enter machine name")

        # Ask which ssh key the user wants to use
        try:
            ssh_key_name = click.prompt(
                "Enter the name of the SSH key you want to use",
                type=click.Choice([key["name"] for key in ssh_keys]),
            )
            # validate that the ssh key name is in the list
            if ssh_key_name not in [key["name"] for key in ssh_keys]:
                raise ValueError("Invalid SSH key name")

        except FileNotFoundError as e:
            logger.error(str(e))
            return

        except Exception as e:
            logger.error(f"Error reading public key file: {e}")
            return

        # Create machine using API
        try:
            if machine_name is None:
                raise ValueError("Machine name cannot be None")

            result = machines_api.create_machine(
                name=machine_name,
                public_key=ssh_key_name,
                region=region,
                cpu_kind=cpu_kind,
                cpu=cpu,
                memory=memory,
                volume_size=volume_size,
            )
            if result:
                created_machine = machines_api.get_machines(machine_name)
                if created_machine:
                    headers = list(created_machine[0].keys())
                    logger.info(make_table_view([created_machine[0]], headers))

        except Exception as e:
            logger.error(f"Error creating machine: {e}")
            return

        # prompt to add to ssh config
        should_add_to_ssh_config = click.confirm(
            "Would you like to add this machine to your SSH config?", default=True
        )
        if should_add_to_ssh_config:
            # add to ssh config
            alias, port = machines_api.get_machine_alias(machine_name)
            if alias is None or port is None:
                logger.error(
                    "Error getting machine alias. Please try again by running `machines connect add <machine-name>`."
                )
                return

            user_id = machines_api.get_user_id()
            ssh_config_manager.add_machine(machine_name, alias, port, user_id)
            logger.success(f"Added machine {machine_name} to SSH config")

    except Exception as e:
        logger.error(f"Error creating machine: {e}")


@machines.command()
@click.argument("machine_name")
@click.option("--cpu-kind", "-k", help="CPU kind")
@click.option("--cpu", "-c", type=int, help="Number of CPUs")
@click.option("--memory", "-m", type=float, help="Memory in GB")
@click.option("--region", "-r", help="Region to deploy the machine")
def scale(
    machine_name: str,
    cpu_kind: Optional[str],
    cpu: Optional[int],
    memory: Optional[int],
    region: Optional[str],
):
    """Scale machine resources"""
    try:
        machines_api.scale_machine(machine_name, cpu_kind, cpu, memory, region)
        logger.success(f"Successfully scaled machine {machine_name}")
    except Exception as e:
        logger.error(f"Error scaling machine: {e}")


@machines.command()
@click.argument("machine_name")
def destroy(machine_name: str):
    """Destroy a machine"""
    try:
        # prompt for confirmation
        should_destroy = click.confirm(
            "Are you sure you want to destroy this machine?", default=False
        )
        if not should_destroy:
            logger.info("Machine not destroyed")
            return

        machines_api.destroy_machine(machine_name)

        # remove from ssh config if it exists
        ssh_config_manager.remove_machine(machine_name)

        logger.success(f"Successfully destroyed machine {machine_name}")

    except Exception as e:
        logger.error(f"Error deleting machine: {e}")


@machines.command()
@click.argument("machine_name", required=False)
def get(machine_name: Optional[str]):
    """Get machine(s). If machine_name is provided, get that specific machine."""
    try:
        machines = machines_api.get_machines(machine_name)

        # Define headers based on the first machine's keys
        if machines:
            headers = list(machines[0].keys())
            logger.info(make_table_view(machines, headers))
        else:
            logger.warning("No machines found")
    except Exception as e:
        logger.error(f"Error getting machine(s): {e}")


@machines.command(name="ls")
def list_machines():
    """List all machines"""
    try:
        machines = machines_api.get_machines()
        if machines:
            headers = list(machines[0].keys())
            logger.info(make_table_view(machines, headers))
        else:
            logger.warning("No machines found")

    except Exception as e:
        logger.error(f"Error listing machines: {e}")


@machines.command(name="connect")
@click.argument("machine_name")
@click.option("--ssh-key", help="Path to SSH private key file")
def connect_machine(machine_name: str, ssh_key: Optional[str]):
    """Connect to a machine"""
    try:
        alias, port = machines_api.get_machine_alias(machine_name)
        if alias:
            if not ssh_key:
                ssh_key = get_default_key_path().replace(
                    ".pub", ""
                )  # Remove .pub extension to get private key

            logger.info(f"Connecting to machine {machine_name}...")
            user_id = machines_api.get_user_id()
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
            logger.warning("No machines found")

    except Exception as e:
        logger.error(f"Error connecting to machine: {e}")
