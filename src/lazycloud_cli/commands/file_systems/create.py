import typer
import click

from lazycloud_cli.api import api
from lazycloud_cli.logging import logger

app = typer.Typer(help="Extend machine file systems")


@app.command()
def create(name: str = typer.Argument(..., help="Name of the file system to create")):
    """Create a new file system"""
    try:
        # Verify file system does not exist
        file_systems = api.file_systems.get_file_system(name)
        if file_systems:
            logger.error(f"File system '{name}' already exists")
            raise typer.Exit(1)

        # NOTE: regions is dependent on the machine options
        machine_options = api.machines.get_machine_options()
        if not machine_options:
            logger.error(
                "No machine options found. Please create a machine first with `lazycloud machines create`"
            )
            return

        # check if the user plans to use a GPU
        using_gpu = logger.confirm(
            "Do you plan to attach a GPU to the machine using this file system?",
            return_result=True,
        )
        if using_gpu:
            # get the specific gpu type from the user
            gpu_kind = logger.option(
                "Which GPU kind do you plan to use?",
                list(machine_options.gpu.keys()),
                default=list(machine_options.gpu.keys())[0],
            )
            regions = machine_options.gpu[gpu_kind].regions
        else:
            gpu_kind = None
            regions = machine_options.regions

        # prompt to get the region
        region = logger.option("Select a region:", regions, default=regions[0])

        # prompt to get the size with validation
        size = typer.prompt(
            "Enter the size of the file system in GB. Minimum size is 10GB.",
            default=10,
            type=click.IntRange(min=10),
        )

        # Create file system
        api.file_systems.create_file_system(name, size, region, gpu_kind)
        logger.success(f"File system '{name}' created successfully")

    except Exception as e:
        logger.error(f"Failed to create file system: {e}")
        raise typer.Exit(1)
