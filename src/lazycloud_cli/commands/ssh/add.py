import typer

from lazycloud_cli.api import ssh_keys_api
from lazycloud_cli.logging import logger


app = typer.Typer(help="Add SSH keys")


@app.command()
def add(
    name: str = typer.Argument(..., help="Name for the SSH key"),
):
    """Add a new SSH key"""
    try:
        # Check for default key if no path provided
        key_value = typer.prompt("Enter the SSH key value")

        # Add the key
        result = ssh_keys_api.create_ssh_key(name, key_value)
        if result:
            logger.success(f"Successfully added SSH key {name}")
        else:
            logger.error(f"Failed to add SSH key {name}")

    except Exception as e:
        logger.error(f"Failed to add SSH key: {e}")
        raise typer.Exit(1)
