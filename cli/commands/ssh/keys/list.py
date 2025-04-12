import typer
from cli.api import api
from cli.logging import logger

app = typer.Typer(help="List SSH keys")


@app.command()
def ls():
    """List all SSH keys"""
    try:
        keys = api.ssh_keys.get_ssh_keys()

        if not keys:
            logger.info("No SSH keys found")
            return

        # Create and display the table
        logger.table(keys, title="SSH Keys")

    except Exception as e:
        logger.error(f"Failed to list SSH keys: {e}")
        raise typer.Exit(1)
