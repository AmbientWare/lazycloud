import typer
from typing import Optional
from click.types import Choice

from cli.api import api
from cli.logging import logger
from cli.ssh_config import ssh_config_manager

app = typer.Typer(help="Create a new machine")

@app.command()
def create(
    machine_name: str = typer.Argument(..., help="Name of the machine to create"),
    region: Optional[str] = typer.Option(None, help="Region to deploy the machine"),
    cpu_kind: Optional[str] = typer.Option(None, help="CPU kind"),
    cpu: Optional[int] = typer.Option(None, help="Number of CPUs"),
    memory: Optional[int] = typer.Option(None, help="Memory in GB"),
    volume_size: Optional[int] = typer.Option(None, help="Volume size in GB"),
):
    """Create a new machine"""
    try:
        ssh_keys = api.ssh_keys.get_ssh_keys()
        if not ssh_keys:
            logger.error(
                "No SSH keys found. Please create an SSH key first with `remach ssh keys add`"
            )
            return

        # Ask which ssh key the user wants to use
        try:
            key_choices = Choice([key["name"] for key in ssh_keys])
            ssh_key_name = typer.prompt(
                "Enter the name of the SSH key you want to use",
                type=key_choices,
                show_choices=True,
            )

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

            result = api.machines.create_machine(
                name=machine_name,
                public_key=ssh_key_name,
                region=region,
                cpu_kind=cpu_kind,
                cpu=cpu,
                memory=memory,
                volume_size=volume_size,
            )
            if result:
                created_machine = api.machines.get_machines(machine_name)
                if created_machine:
                    logger.table(created_machine)

        except Exception as e:
            logger.error(f"Error creating machine: {e}")
            return

        # prompt to add to ssh config
        should_add_to_ssh_config = typer.confirm(
            "Would you like to add this machine to your SSH config?",
            default=True,
        )
        if should_add_to_ssh_config:
            # add to ssh config
            alias, port = api.machines.get_machine_alias(machine_name)
            if alias is None or port is None:
                logger.error(
                    "Error getting machine alias. Please try again by running `machines connect add <machine-name>`."
                )
                return

            user_id = api.users.get_user_id()
            ssh_config_manager.add_machine(machine_name, alias, port, user_id)
            logger.success(f"Added machine {machine_name} to SSH config")

    except Exception as e:
        logger.error(f"Error creating machine: {e}")
