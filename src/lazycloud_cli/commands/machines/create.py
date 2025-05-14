import typer
from rich.console import Console
import click

from lazycloud_cli.api import api
from lazycloud_cli.logging import logger
from lazycloud_cli.ssh_config import ssh_config_manager

app = typer.Typer(help="Create a new machine")
console = Console()


@app.command()
def create(
    name: str = typer.Argument(..., help="Name of the machine to create"),
):
    """Create a new machine"""
    try:
        # make sure the machine name is not already taken
        if api.machines.get_machines(name):
            logger.error(
                f"Machine '{name}' already exists. Please choose a different name."
            )
            return

        ssh_keys = api.ssh_keys.get_ssh_keys()
        if not ssh_keys:
            logger.error(
                "No SSH keys found. Please create an SSH key first with `lazycloud ssh add`"
            )
            return

        machine_options = api.machines.get_machine_options()
        if not machine_options:
            logger.error(
                "No machine options found. Please create a machine first with `lazycloud machines create`"
            )
            return

        # prompt to get the gpu kind
        gpu_kind = logger.option(
            "Select a GPU to attach to the machine, otherwise leave blank",
            list(machine_options.gpu.keys()),
            default="None",
        )

        # prompt to get the region
        if gpu_kind != "None":
            # GPU regions are specific to the GPU kind
            regions = machine_options.gpu[gpu_kind].regions
        else:
            # There are several more supported regions for CPU only machines
            gpu_kind = None # later methods expect gpu_kind to be None if no GPU is selected
            regions = machine_options.regions

        region = logger.option(
            "Select a region to deploy the machine:", regions, default=regions[0]
        )

        # prompt to get the cpu
        cpu_options = list(machine_options.compute.keys())
        cpu = logger.option(
            "Select how many CPU cores to allocate:",
            [str(cpu) for cpu in cpu_options],
            default=cpu_options[0],
        )

        # prompt to get the memory
        memory_options = machine_options.compute[cpu]
        memory = logger.option(
            "Select how much RAM to allocate (GB):",
            [str(memory) for memory in memory_options],
            default=memory_options[0],
        )

        # Ask which ssh key the user wants to use
        try:
            key_names = [key["name"] for key in ssh_keys]
            ssh_key_name = logger.option(
                "Select the SSH key to use for machine authentication:",
                key_names,
            )

        except FileNotFoundError as e:
            logger.error(str(e))
            return

        except Exception as e:
            logger.error(f"Error reading public key file: {e}")
            return

        # have user input the file system size, default to 10
        file_systems = api.file_systems.get_available_file_systems()
        # only keep file systems in the selected region
        file_systems = [fs for fs in file_systems if fs["region"] == region]
        if file_systems:
            fs_names = [fs["name"] for fs in file_systems]
            file_system_name = logger.option(
                "Select a file system or leave blank to create a new one (only shows file systems in the selected region)",
                fs_names,
                default="Create New",
            )
        else:
            logger.warning(
                "No file systems found. you will need to create a file system first."
            )
            file_system_name = "Create New"

        if file_system_name == "Create New":
            file_system_name = typer.prompt("Enter the name of the file system")
            file_system_size = typer.prompt(
                "Enter the size of the file system in GB. Minimum size is 10GB.",
                default=10,
                type=click.IntRange(min=10),
            )
            # create a new file system if wants to
            file_system = api.file_systems.create_file_system(
                file_system_name, file_system_size, region, gpu_kind
            )

        else:
            file_system = api.file_systems.get_file_system(file_system_name)

        fs_id = file_system.get("id")
        if not fs_id:
            logger.error(f"File system '{file_system_name}' not found")
            return

        # Create machine using API
        try:
            result = api.machines.create_machine(
                name=name,
                public_key=ssh_key_name,
                region=region,
                cpu=int(cpu),
                memory=int(memory),
                file_system_id=fs_id,
                gpu_kind=gpu_kind if gpu_kind != "None" else None,
            )

            if result:
                created_machine = api.machines.get_machines(name)
                if created_machine:
                    logger.table(created_machine)

        except Exception as e:
            logger.error(f"Error creating machine: {e}")
            return

        # add to ssh config
        ip_address, port = api.machines.connection_details(name)
        if ip_address is None or port is None:
            logger.error(
                "Error getting machine IP address and port. Please try again by running `machine connect add <machine-name>`."
            )
            return

        ssh_config_manager.add_machine(name, ip_address, port)
        logger.success(f"Added machine {name} to SSH config")
        logger.success(
            f"Machine created successfully. You can now connect to it using `lazycloud machine connect {name}`"
        )

    except Exception as e:
        logger.error(f"Error creating machine: {e}")
