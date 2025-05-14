import typer

from lazycloud_cli.api import api
from lazycloud_cli.api.utils import mb_to_gb
from lazycloud_cli.logging import logger

app = typer.Typer(help="Scale a machine's resources")


@app.command()
def scale(
    name: str = typer.Argument(..., help="Name of the machine to scale"),
):
    """Scale a machine's resources"""
    try:
        # Get current machine details
        current_machine = api.machines.get_machines(name)
        if not current_machine:
            logger.error(f"Machine {name} not found")
            return

        # Get available machine options
        machine_options = api.machines.get_machine_options()
        if not machine_options:
            logger.error("No machine options found")
            return

        # Get current values
        current_cpu = current_machine[0].get("cpu", 1)
        current_memory = current_machine[0].get("memory", 2)

        # Get CPU options
        cpu_options = list(machine_options.compute.keys())
        cpu_options = [opt for opt in cpu_options if int(opt) != int(current_cpu)]
        cpu = logger.option(
            "Available CPU options (Current: " + str(current_cpu) + "):",
            cpu_options,
            default=str(cpu_options[0]),
        )

        # Get memory options for the selected CPU
        memory_options = [
            opt
            for opt in machine_options.compute[str(cpu)]
            if int(opt) != int(current_memory)
        ]
        memory = logger.option(
            "Available RAM options (Current: " + str(mb_to_gb(current_memory)) + "GB):",
            memory_options,
            default=str(memory_options[0]),
        )

        # Check if at least one resource is being changed
        if cpu == current_cpu and memory == current_memory:
            logger.warning(
                "CPU and RAM values are the same as current values. Please specify a different value."
            )
            return

        result = api.machines.scale_machine(
            machine_name=name,
            cpu=int(cpu),
            memory=int(memory),
        )

        if result:
            scaled_machine = api.machines.get_machines(name)
            if scaled_machine:
                logger.table(scaled_machine)
                logger.success(
                    f"Successfully scaled machine {name} to {cpu} CPU and {memory} GB RAM"
                )

    except Exception as e:
        logger.error(f"Error scaling machine: {e}")
