import typer
from rich.console import Console
from rich.table import Table
from rich.box import ROUNDED

from lazycloud_cli.api import api
from lazycloud_cli.logging import logger

app = typer.Typer(help="Create a new machine")
console = Console()


def create_compute_table(compute_options: dict, regions: list) -> Table:
    """Create a table showing CPU and memory options"""
    table = Table(
        show_header=True,
        header_style="bold cyan",
        title="Compute Options",
        title_style="bold magenta",
        title_justify="left",
        border_style="blue",
        box=ROUNDED,
    )

    table.add_column("CPU Cores", style="cyan")
    table.add_column("Memory Options (GB)", style="green")
    table.add_column("Available Regions", style="yellow")

    # Format regions nicely
    regions_str = ", ".join(sorted(regions))

    for cpu_cores, memory_options in compute_options.items():
        memory_str = ", ".join(str(mem) for mem in memory_options)
        table.add_row(cpu_cores, memory_str, regions_str)

    return table


def create_gpu_table(gpu_options: dict) -> Table:
    """Create a table showing GPU options"""
    table = Table(
        show_header=True,
        header_style="bold cyan",
        title="GPU Options",
        title_style="bold magenta",
        title_justify="left",
        border_style="blue",
        box=ROUNDED,
    )

    table.add_column("GPU Model", style="cyan")
    table.add_column("Available Regions", style="yellow")

    for gpu_model, info in gpu_options.items():
        # Sort and format regions for consistency
        regions_str = ", ".join(sorted(info.regions))
        table.add_row(gpu_model, regions_str)

    return table


@app.command()
def options():
    """Display available machine options"""
    machine_options = api.machines.get_machine_options()
    if not machine_options:
        logger.error(
            "No machine options found. Please check your connection and try again."
        )
        return

    # Display compute options
    compute_table = create_compute_table(
        machine_options.compute, machine_options.regions
    )
    logger.print(compute_table)
    logger.print()  # Empty line for spacing

    # Display GPU options
    gpu_table = create_gpu_table(machine_options.gpu)
    logger.print(gpu_table)
