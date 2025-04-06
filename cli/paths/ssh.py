import click

from cli.paths.remachina import remach
from cli.logging import logger
from cli.api import machines_api
from cli.ssh_config import ssh_config_manager
from cli.paths.utils import make_table_view
import os


@remach.group()
def ssh():
    """SSH management commands"""
    pass


@ssh.group()
def keys():
    """SSH key management commands"""
    pass


@ssh.group()
def config():
    """SSH config management commands"""
    pass


## SSH key commands


@keys.command(name="ls")
def list():
    """List all SSH keys"""
    public_keys = machines_api.get_ssh_keys()
    if not public_keys:
        logger.error(
            "No SSH keys found. Please create an SSH key first with `remach ssh keys add`"
        )
        return

    # Format the data for table display
    data = []
    for key in public_keys:
        value = key.get("public_key", "")
        if value:
            value = value[:24] + "..." if len(value) > 24 else value
        data.append({"Name": key.get("name", ""), "Value": value})

    logger.info(make_table_view(data, ["Name", "Value"]))


@keys.command(name="add")
@click.argument("name", required=True)
def add_key(name: str):
    """Add a new SSH key"""
    # check for any keys that are in the normal ~/.ssh/id_rsa.pub
    default_key = os.path.expanduser("~/.ssh/id_rsa.pub")
    if os.path.exists(default_key):
        use_default_key = click.confirm(
            "Do you want to use the default key in ~/.ssh/id_rsa.pub?", default=True
        )
        if use_default_key:
            with open(default_key, "r") as f:
                public_key = f.read()

    else:
        # prompt for the public key
        public_key_path = click.prompt(
            "Enter path to public key you want to add (example: ~/.ssh/id_rsa.pub)"
        )
        if "~" in public_key_path:
            public_key_path = os.path.expanduser(public_key_path)

        if not os.path.exists(public_key_path):
            logger.error(f"File {public_key_path} does not exist")
            return

        with open(public_key_path, "r") as f:
            public_key = f.read()

    machines_api.create_ssh_key(name, public_key)
    logger.success(f"Successfully added SSH key {name}")


@keys.command(name="rm")
@click.argument("name")
def rm_key(name: str):
    """Remove an SSH key"""
    ssh_keys = machines_api.get_ssh_keys()
    if not ssh_keys:
        logger.error(
            "No SSH keys found. Please create an SSH key first with `remach ssh keys add`"
        )
        return

    ssh_key = next((key for key in ssh_keys if key["name"] == name), None)
    if ssh_key is None:
        logger.error(f"SSH key {name} not found")
        return

    key_id = ssh_key.get("id")
    if key_id is None:
        logger.error(f"SSH key {name} not found")
        return

    machines_api.delete_ssh_key(key_id)
    logger.success(f"Successfully removed SSH key {name}")


## SSH config commands


@config.command(name="add")
@click.argument("machine_name")
def add_machine_to_config(machine_name: str):
    """Add a machine to SSH config"""
    try:
        alias, port = machines_api.get_machine_alias(machine_name)
        if alias is None or port is None:
            logger.error(
                "Error getting machine alias. Please make sure the machine exists."
            )
            return

        user_id = machines_api.get_user_id()
        ssh_config_manager.add_machine(machine_name, alias, port, user_id)
        logger.success(f"Successfully added machine {machine_name} to SSH config")

    except Exception as e:
        logger.error(f"Error adding machine to SSH config: {e}")


@config.command(name="rm")
@click.argument("machine_name")
def rm_machine_from_config(machine_name: str):
    """Remove a machine from SSH config"""
    ssh_config_manager.remove_machine(machine_name)
    logger.success(f"Successfully removed machine {machine_name} from SSH config")
