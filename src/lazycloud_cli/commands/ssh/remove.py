import typer

from lazycloud_cli.api import ssh_keys_api
from lazycloud_cli.logging import logger


app = typer.Typer(help="Remove SSH keys")


@app.command()
def rm(name: str = typer.Argument(..., help="Name of the SSH key to remove")):
    """Remove an SSH key"""
    try:
        # Get all keys to find the ID
        keys = ssh_keys_api.get_ssh_keys()
        if not keys:
            logger.error("No SSH keys found")
            raise typer.Exit(1)

        # Find the key with the given name
        key = next((k for k in keys if k.get("name") == name), None)
        if not key:
            logger.error(f"SSH key {name} not found")
            raise typer.Exit(1)

        # Get the key ID
        key_id = key.get("id")
        if not key_id:
            logger.error(f"SSH key {name} has no ID")
            raise typer.Exit(1)

        # Remove the key
        if ssh_keys_api.delete_ssh_key(key_id):
            logger.success(f"Successfully removed SSH key {name}")
        else:
            logger.error(f"Failed to remove SSH key {name}")

    except Exception as e:
        logger.error(f"Failed to remove SSH key: {e}")
        raise typer.Exit(1)
