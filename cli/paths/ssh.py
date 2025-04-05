import click

from cli.paths.remachina import remach
from cli.logging import logger
from cli.api import MachineAPI
from cli.ssh_config import ssh_config_manager


@remach.group()
def ssh():
    """SSH management commands"""
    pass


@ssh.command()
@click.argument("machine_name")
def add(machine_name: str):
    """Add a machine to SSH config"""
    try:
        api = MachineAPI()
        alias, port = api.get_machine_alias(machine_name)
        if alias is None or port is None:
            logger.error(
                "Error getting machine alias. Please make sure the machine exists."
            )
            return

        user_id = api.get_user_id()
        ssh_config_manager.add_machine(machine_name, alias, port, user_id)
        logger.success(f"Successfully added machine {machine_name} to SSH config")

    except Exception as e:
        logger.error(f"Error adding machine to SSH config: {e}")


@ssh.command()
@click.argument("machine_name")
def remove(machine_name: str):
    """Remove a machine from SSH config"""
    ssh_config_manager.remove_machine(machine_name)
    logger.success(f"Successfully removed machine {machine_name} from SSH config")
