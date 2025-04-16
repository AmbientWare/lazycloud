import subprocess
from pathlib import Path
import json

from machines.config import app_config
from machines.services.fly.schemas import (
    AppConfig,
    CheckStatus,
    FlyMachineConfig,
    RESOURCE_MAP,
    IMAGE_MAP,
)
from machines.services.fly.utils import run_async_command, deploying_status_callback
from machines.services.aws.route53 import Route53Service
from machines.database import db
from machines.database.machines import MachineStatus


class FlyAppManager:
    def __init__(self, org_name: str = app_config.FLY_ORG_NAME):
        """Initialize the Fly.io app manager.

        Args:
            org_name: The Fly.io organization name
            base_dir: The base directory for app files. Defaults to the directory containing this file.
        """
        self.route_53 = Route53Service()
        self.org_name = org_name
        self.base_dir = Path(__file__).resolve().parent

    async def get_app_name(self, usage_uuid: str, machine_id: int) -> str:
        """Get the name of the Fly.io application."""
        return f"lc-{usage_uuid}-{machine_id}"

    async def _add_authorized_keys(self, config: AppConfig) -> None:
        """Add SSH authorized keys to the application."""
        pub_key = config.public_key.strip()
        if not pub_key:
            raise ValueError("SSH key file is empty")

        print(f"Adding secrets to app {await self.get_app_name(config.usage_uuid, config.machine_id)}")
        try:
            await run_async_command(
                [
                    "fly",
                    "secrets",
                    "set",
                    f"AUTHORIZED_KEYS={pub_key}",
                    "-a",
                    await self.get_app_name(config.usage_uuid, config.machine_id),
                ]
            )
        except subprocess.CalledProcessError as e:
            if "already exists" in e.stderr:
                print(
                    f"Secret {await self.get_app_name(config.usage_uuid, config.machine_id)} already exists"
                )
            else:
                raise e

    async def create_app(self, config: AppConfig) -> None:
        """Create a new Fly.io application."""
        print(f"Creating app {await self.get_app_name(config.usage_uuid, config.machine_id)}")
        # Create the app
        try:
            await db.machines.update_machine_status(
                config.machine_id, MachineStatus.INITIALIZED
            )
            await run_async_command(
                [
                    "fly",
                    "apps",
                    "create",
                    await self.get_app_name(config.usage_uuid, config.machine_id),
                    "--org",
                    self.org_name,
                    "--network",
                    config.network,
                ]
            )

        except Exception as e:
            if "already been taken" in str(e):
                print(
                    f"App {await self.get_app_name(config.usage_uuid, config.machine_id)} already exists"
                )
            else:
                raise e

        try:
            await db.machines.update_machine_status(
                config.machine_id, MachineStatus.NETWORKING
            )
            # Allocate IPv4 address
            await run_async_command(
                [
                    "fly",
                    "ips",
                    "allocate-v4",
                    "--app",
                    await self.get_app_name(config.usage_uuid, config.machine_id),
                    "--yes",
                ]
            )
        except subprocess.CalledProcessError as e:
            if "already exists" in e.stderr:
                print(
                    f"IPv4 address for app {await self.get_app_name(config.usage_uuid, config.machine_id)} already exists"
                )
            else:
                raise e

        # add the authorized key so that we can ssh into the machine
        await self._add_authorized_keys(config)

    async def delete_app(self, usage_uuid: str, machine_id: int) -> None:
        """Delete a Fly.io application."""
        print(f"Deleting app {await self.get_app_name(usage_uuid, machine_id)}")
        await db.machines.update_machine_status(machine_id, MachineStatus.DELETING)
        await run_async_command(
            [
                "fly",
                "apps",
                "destroy",
                await self.get_app_name(usage_uuid, machine_id),
                "--yes",
            ]
        )

        # now try to delete the CNAME record from Route53
        await self.route_53.delete_cname_record(
            await self.get_app_name(usage_uuid, machine_id),
        )

        # finally, delete the machine from the database
        await db.machines.adelete(machine_id)

    async def deploy_app(
        self,
        machine_config: FlyMachineConfig,
    ) -> None:
        """Deploy the application to Fly.io."""
        print(f"Deploying app {await self.get_app_name(machine_config.usage_uuid, machine_config.machine_id)}")

        # now try to add a CNAME record to the app in Route53
        try:
            await self.route_53.create_cname_record(
                await self.get_app_name(machine_config.usage_uuid, machine_config.machine_id),
            )

        except Exception as e:
            raise e

        image = IMAGE_MAP.get(machine_config.image_type)
        if not image:
            raise ValueError(
                f"Invalid image type: {machine_config.image_type}. Must be one of {IMAGE_MAP.keys()}"
            )

        fly_toml_path = self.base_dir / "app_files" / "fly.toml"
        await run_async_command(
            [
                "fly",
                "deploy",
                "--yes",
                "-a",
                await self.get_app_name(machine_config.usage_uuid, machine_config.machine_id),
                "-c",
                str(fly_toml_path),
                "--image",
                image,
                "--primary-region",
                machine_config.region.value,
                "--vm-cpu-kind",
                machine_config.cpu_kind,
                "--vm-cpus",
                str(machine_config.cpu),
                "--vm-memory",
                str(machine_config.memory),
                "--volume-initial-size",
                str(machine_config.initial_volume_size),
                "--vm-gpu-kind",
                machine_config.gpu_kind if machine_config.gpu_kind else "none",
                "--ha=false",
            ],
            stdout_callback=lambda line: deploying_status_callback(
                machine_config.machine_id, line
            ),
        )

        await db.machines.update_machine_status(
            machine_config.machine_id, MachineStatus.DEPLOYED
        )

    async def scale_app(
        self, usage_uuid: str, machine_id: int, cpu_kind: str, cpu: int, memory: int
    ) -> None:
        """Scale the application to the given number of machines."""
        print(
            f"Scaling app {await self.get_app_name(usage_uuid, machine_id)} to {cpu_kind} {cpu} {memory}"
        )
        vm_type = RESOURCE_MAP.get(cpu_kind)
        if not vm_type:
            raise ValueError(
                f"Invalid CPU kind: {cpu_kind}. Must be one of {RESOURCE_MAP.keys()}"
            )

        vm_config = vm_type.get(cpu)
        if not vm_config:
            raise ValueError(
                f"Invalid CPU size: {cpu} for kind: {cpu_kind}. Must be one of {vm_type.keys()}"
            )

        if memory not in vm_config["memory_options"]:
            memory_gb = memory / 1024
            valid_memory_gb = [f"{opt/1024:.1f}" for opt in vm_config["memory_options"]]
            raise ValueError(
                f"Invalid memory size: {memory_gb:.2f}GB for {vm_config['name']}. "
                f"Must be one of {', '.join(valid_memory_gb)}GB"
            )

        await run_async_command(
            [
                "fly",
                "scale",
                "vm",
                vm_config["name"],
                "-a",
                await self.get_app_name(usage_uuid, machine_id),
                "--vm-memory",
                str(memory),
            ]
        )

    async def extend_volume(self, usage_uuid: str, machine_id: int, volume_size: int) -> None:
        """Extend the volume of the application."""
        print(
            f"Extending volume of app {await self.get_app_name(usage_uuid, machine_id)} to {volume_size}GB"
        )

        try:
            # get the volume id
            response = await run_async_command(
                [
                    "fly",
                    "volume",
                    "list",
                    "-a",
                    await self.get_app_name(usage_uuid, machine_id),
                    "--json",
                ],
                print_output=False,
            )

            volumes = json.loads(response.stdout)
            for volume in volumes:
                if volume["name"] == "persistant_data":
                    volume_id = volume["id"]
                    break

            if not volume_id:
                raise ValueError(
                    f"Volume {await self.get_app_name(usage_uuid, machine_id)} not found"
                )

        except subprocess.CalledProcessError as e:
            raise e

        try:
            await run_async_command(
                [
                    "fly",
                    "volume",
                    "extend",
                    volume_id,
                    "-s",
                    str(volume_size),
                    "-a",
                    await self.get_app_name(usage_uuid, machine_id),
                ]
            )

        except subprocess.CalledProcessError as e:
            raise e

    async def get_check_status(self, usage_uuid: str, machine_id: int) -> CheckStatus:
        """Get the status of the check for the application."""
        app_name = await self.get_app_name(usage_uuid, machine_id)
        response = await run_async_command(
            [
                "fly",
                "machines",
                "list",
                "-a",
                app_name,
                "--json",
            ],
            print_output=False,
        )
        machines = json.loads(response.stdout)
        for machine in machines:
            checks = machine.get("checks", {})
            ssh_check = checks.get("sshCheck", {})
            if ssh_check.get("status") == "passing":
                return CheckStatus.PASSING

            elif ssh_check.get("status") == "failing":
                return CheckStatus.FAILING

            else:
                return CheckStatus.UNKNOWN

        return CheckStatus.UNKNOWN
