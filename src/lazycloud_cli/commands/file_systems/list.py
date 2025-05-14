import typer

from lazycloud_cli.api import api
from lazycloud_cli.logging import logger

app = typer.Typer(help="List file systems")


@app.command()
def ls():
    """List all file systems"""
    try:
        # Get file systems from API
        file_systems = api.file_systems.list_file_systems()

        if not file_systems:
            logger.warning("No file systems found")
            return

        # Display the table
        logger.table(file_systems)

    except Exception as e:
        logger.error(f"Failed to list file systems: {e}")
        raise typer.Exit(1)
