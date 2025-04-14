import typer
import subprocess
from cli.api import api
from cli.config import config
from cli.logging import logger
from cli.ssh_config import ssh_config_manager

app = typer.Typer(help="Connect to machines")


@app.command()
def connect(
    machine_name: str = typer.Argument(..., help="Name of the machine to connect to"),
    command: str = typer.Option(
        None, "--command", "-c", help="Command to execute on the remote machine"
    ),
):
    """Connect to a machine via SSH"""
    try:
        # Get machine details from API
        machines = api.machines.get_machines(machine_name)
        if not machines:
            logger.error(f"Machine '{machine_name}' not found")
            raise typer.Exit(1)

        machine = machines[0]  # Get the first (and should be only) machine

        # Get machine connection details
        alias, port = api.machines.get_machine_alias(machine_name)
        if not alias or not port:
            logger.error(
                f"Failed to get connection details for machine '{machine_name}'"
            )
            raise typer.Exit(1)

        # Ensure SSH config is up to date
        ssh_config_manager.add_machine(
            machine_name=machine_name,
            alias=alias,
            port=port,
        )

        # Build SSH command
        ssh_cmd = ["ssh", machine_name]
        if command:
            ssh_cmd.extend(["-t", command])

        # Execute SSH command
        logger.status(f"Connecting to {machine_name}...")
        subprocess.run(ssh_cmd)

    except Exception as e:
        logger.error(f"Failed to connect to {machine_name}: {e}")
        raise typer.Exit(1)
