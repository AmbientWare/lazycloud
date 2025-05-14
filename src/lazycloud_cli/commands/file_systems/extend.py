import typer

from lazycloud_cli.api import api
from lazycloud_cli.logging import logger

app = typer.Typer(help="Extend machine file systems")


@app.command()
def extend(name: str = typer.Argument(..., help="Name of the file system to extend")):
    """Extend a file system to a specific size in GB"""
    # Verify file system exists
    try:
        file_system = api.file_systems.get_file_system(name)
    except Exception as e:
        logger.error(f"Could not find file system '{name}'")
        raise typer.Exit(1)

    new_size = typer.prompt(
        f"Current size of {name} is {file_system['size']} GB. Enter the new size for the file system in GB"
    )

    # Validate user want to expand, it is irreversible
    logger.confirm(
        f"Are you sure you want to extend file system '{name}' to {new_size}GB? This action is irreversible."
    )

    try:
        # Extend the file system
        api.file_systems.extend_file_system(name, new_size)
        logger.success(f"File system '{name}' extended to {new_size}GB")

    except Exception as e:
        logger.error(f"Failed to extend file system: {e}")
        raise typer.Exit(1)
