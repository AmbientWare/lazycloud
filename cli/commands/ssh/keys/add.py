import os
import typer
from cli.api import ssh_keys_api
from cli.logging import logger


app = typer.Typer(help="Add SSH keys")


@app.command()
def add(
    name: str = typer.Option(..., prompt="Name for the SSH key", help="Name for the SSH key"),
    key_path: str = typer.Option(
        None,
        "--path",
        "-p",
        help="Path to the public key file (defaults to ~/.ssh/id_rsa.pub)",
    ),
):
    """Add a new SSH key"""
    try:
        # Check for default key if no path provided
        if not key_path:
            default_key = os.path.expanduser("~/.ssh/id_rsa.pub")
            if os.path.exists(default_key):
                use_default = typer.confirm(
                    "Do you want to use the default key in ~/.ssh/id_rsa.pub?",
                    default=True,
                )
                if use_default:
                    key_path = default_key
                else:
                    key_path = typer.prompt(
                        "Enter path to public key (example: ~/.ssh/id_rsa.pub)"
                    )
            else:
                key_path = typer.prompt(
                    "Enter path to public key (example: ~/.ssh/id_rsa.pub)"
                )

        # Expand user path if needed
        if "~" in key_path:
            key_path = os.path.expanduser(key_path)

        # Check if file exists
        if not os.path.exists(key_path):
            logger.error(f"File {key_path} does not exist")
            raise typer.Exit(1)

        # Read the public key
        with open(key_path, "r") as f:
            public_key = f.read().strip()

        # Add the key
        result = ssh_keys_api.create_ssh_key(name, public_key)
        if result:
            logger.success(f"Successfully added SSH key {name}")
        else:
            logger.error(f"Failed to add SSH key {name}")

    except Exception as e:
        logger.error(f"Failed to add SSH key: {e}")
        raise typer.Exit(1)
