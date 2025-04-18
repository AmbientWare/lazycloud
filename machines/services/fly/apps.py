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
from machines.services.fly.schemas import FlyRegion


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

    async def _get_machine_id(self, usage_uuid: str, machine_id: int) -> str:
        """Get the machine id for the application."""
        response = await run_async_command(
            [
                "fly",
                "machines",
                "list",
                "-a",
                await self.get_app_name(usage_uuid),
                "--json",
            ],
            print_output=False,
        )

        machine_name = await self.get_machine_name(machine_id)

        machines = json.loads(response.stdout)
        for machine in machines:
            if machine.get("name") == machine_name:
                return machine.get("id")

        raise ValueError(f"Machine {machine_name} not found")

    async def _get_volume_id(self, usage_uuid: str, volume_id: int) -> str:
        """Get the volume id for the application."""
        response = await run_async_command(
            [
                "fly",
                "volume",
                "list",
                "-a",
                await self.get_app_name(usage_uuid),
                "--json",
            ],
            print_output=False,
        )

        volumes = json.loads(response.stdout)
        for volume in volumes:
            if volume.get("name") == await self.get_app_volume_name(volume_id):
                return volume.get("id")

        raise ValueError(
            f"Volume {await self.get_app_volume_name(volume_id)} not found"
        )

    async def _add_authorized_keys(self, config: AppConfig) -> None:
        """Add SSH authorized keys to the application."""
        pub_key = config.public_key.strip()
        if not pub_key:
            raise ValueError("SSH key file is empty")

        print(f"Adding secrets to app {await self.get_app_name(config.usage_uuid)}")
        try:
            await run_async_command(
                [
                    "fly",
                    "secrets",
                    "set",
                    f"AUTHORIZED_KEYS={pub_key}",
                    "-a",
                    await self.get_app_name(config.usage_uuid),
                ]
            )
        except subprocess.CalledProcessError as e:
            if "already exists" in e.stderr:
                print(
                    f"Secret {await self.get_app_name(config.usage_uuid)} already exists"
                )
            else:
                raise e

    async def check_app_exists(self, usage_uuid: str):
        app_name = await self.get_app_name(usage_uuid)
        response = await run_async_command(
            ["fly", "apps", "list", "--org", self.org_name, "--json"],
            print_output=False,
        )
        apps = json.loads(response.stdout)
        return app_name in [app.get("Name") for app in apps]

    async def _get_allocated_ip_address(self, usage_uuid: str) -> str | None:
        """Get the allocated IP address for the application."""
        app_name = await self.get_app_name(usage_uuid)
        response = await run_async_command(
            ["fly", "ips", "list", "--app", app_name, "--json"],
            print_output=False,
        )
        ips = json.loads(response.stdout)
        if len(ips) > 0:
            return ips[0].get("Address")

        return None

    async def get_app_name(self, usage_uuid: str) -> str:
        """Get the name of the Fly.io application."""
        return f"lc-{usage_uuid}"

    async def get_machine_name(self, machine_id: int) -> str:
        """Get the name of the Fly.io machine."""
        return f"lc_machine_{machine_id}"

    async def get_app_volume_name(self, volume_id: int) -> str:
        """Get the name of the Fly.io application volume."""
        return f"lc_volume_{volume_id}"

    async def get_volume_info(self, volume_name: str) -> tuple[int, str]:
        """Get the volume size of the Fly.io application."""
        response = await run_async_command(
            ["fly", "volume", "list", "-a", volume_name, "--json"], print_output=False
        )

        volumes = json.loads(response.stdout)
        for volume in volumes:
            if volume.get("name") == volume_name:
                return volume.get("size_gb"), volume.get("region")

        raise ValueError(f"Error getting volume size for {volume_name}")

    async def _allocate_ip_address(self, usage_uuid: str) -> None:
        """Allocate an IP address for the application."""
        app_name = await self.get_app_name(usage_uuid)
        await run_async_command(
            ["fly", "ips", "allocate-v4", "--app", app_name, "--yes"]
        )

        # now try to add a CNAME record to the app in Route53
        try:
            await self.route_53.create_cname_record(
                await self.get_app_name(usage_uuid),
            )

        except Exception as e:
            raise e

    async def release_ip_address(self, usage_uuid: str) -> None:
        """Release an IP address for the application."""
        app_name = await self.get_app_name(usage_uuid)
        ip_address = await self._get_allocated_ip_address(usage_uuid)
        if ip_address:
            await run_async_command(
                ["fly", "ips", "release", ip_address, "--app", app_name]
            )

        await self.route_53.delete_cname_record(
            await self.get_app_name(usage_uuid),
        )

    async def create_app(self, config: AppConfig) -> None:
        """Create a new Fly.io application."""
        print(f"Creating app {await self.get_app_name(config.usage_uuid)}")
        # Create the app
        try:
            await run_async_command(
                [
                    "fly",
                    "apps",
                    "create",
                    await self.get_app_name(config.usage_uuid),
                    "--org",
                    self.org_name,
                    "--network",
                    config.network,
                ]
            )

        except Exception as e:
            if "already been taken" in str(e):
                print(
                    f"App {await self.get_app_name(config.usage_uuid)} already exists"
                )
            else:
                raise e

        try:
            # Allocate IPv4 address
            await self._allocate_ip_address(config.usage_uuid)

        except subprocess.CalledProcessError as e:
            if "already exists" in e.stderr:
                print(
                    f"IPv4 address for app {await self.get_app_name(config.usage_uuid)} already exists"
                )
            else:
                raise e

        # add the authorized key so that we can ssh into the machine
        await self._add_authorized_keys(config)

    async def destroy_app(self, usage_uuid: str) -> None:
        """Delete a Fly.io application."""
        print(f"Deleting app {await self.get_app_name(usage_uuid)}")

        # also release the IP address as this also deletes the CNAME record
        await self.release_ip_address(usage_uuid)

        await run_async_command(
            [
                "fly",
                "apps",
                "destroy",
                await self.get_app_name(usage_uuid),
                "--yes",
            ]
        )

    async def create_file_system(
        self,
        usage_uuid: str,
        volume_id: int,
        size: int,
        region: FlyRegion,
    ) -> str:
        """Create a file system for the application."""
        print(f"Creating file system for {volume_id}")
        volume_name = await self.get_app_volume_name(volume_id)
        await run_async_command(
            [
                "fly",
                "volume",
                "create",
                volume_name,
                "--size",
                str(size),
                "--app",
                await self.get_app_name(usage_uuid),
                "--region",
                region.value,
                "--yes",
            ]
        )

        return volume_name

    async def destroy_file_system(self, usage_uuid: str, volume_id: int) -> None:
        """Destroy a file system for the application."""
        print(f"Destroying file system for {volume_id}")
        fly_volume_id = await self._get_volume_id(usage_uuid, volume_id)
        await run_async_command(["fly", "volume", "destroy", fly_volume_id, "--yes"])

        await db.file_systems.adelete(volume_id)

    async def create_machine(
        self,
        machine_config: FlyMachineConfig,
    ) -> None:
        """Deploy the application to Fly.io."""
        print(f"Deploying app {await self.get_app_name(machine_config.usage_uuid)}")

        await db.machines.update_machine_status(
            machine_config.machine_id, MachineStatus.INITIALIZED
        )

        image = IMAGE_MAP.get(machine_config.image_type)
        if not image:
            raise ValueError(
                f"Invalid image type: {machine_config.image_type}. Must be one of {IMAGE_MAP.keys()}"
            )

        file_system_name = await self.get_app_volume_name(machine_config.file_system_id)

        await run_async_command(
            [
                "fly",
                "machine",
                "run",
                image,
                "--name",
                await self.get_machine_name(machine_config.machine_id),
                "-a",
                await self.get_app_name(machine_config.usage_uuid),
                "--port",
                # ssh is exposed on port 2222 on the machine
                f"{machine_config.port}:2222/tcp",
                "--region",
                machine_config.region.value,
                "--vm-cpu-kind",
                machine_config.cpu_kind,
                "--vm-cpus",
                str(machine_config.cpu),
                "--vm-memory",
                str(machine_config.memory),
                "--volume",
                # mount the volume to /data on the machine
                file_system_name + ":data",
            ],
            stdout_callback=lambda line: deploying_status_callback(
                machine_config.machine_id, line
            ),
        )

        await db.machines.update_machine_status(
            machine_config.machine_id, MachineStatus.DEPLOYED
        )

    async def destroy_machine(self, usage_uuid: str, machine_id: int) -> None:
        """Destroy the machine."""
        print(f"Destroying machine {await self.get_machine_name(machine_id)}")
        await db.machines.update_machine_status(machine_id, MachineStatus.DELETING)
        # get the machine id
        fly_machine_id = await self._get_machine_id(usage_uuid, machine_id)
        await run_async_command(
            [
                "fly",
                "machine",
                "destroy",
                fly_machine_id,
                "--force",
            ]
        )

        # finally, delete the machine from the database
        await db.machines.adelete(machine_id)

    async def scale_machine(
        self, usage_uuid: str, machine_id: int, cpu_kind: str, cpu: int, memory: int
    ) -> None:
        """Scale the application to the given number of machines."""
        print(
            f"Scaling machine {await self.get_machine_name(machine_id)} to {cpu_kind} {cpu} {memory}"
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

        ## get the machine id
        fly_machine_id = await self._get_machine_id(usage_uuid, machine_id)

        await run_async_command(
            [
                "fly",
                "machine",
                "update",
                fly_machine_id,
                "--vm-size",
                vm_config["name"],
                "--vm-memory",
                str(memory),
                "--vm-gpu-kind",
                "a10",
                "--yes",
            ]
        )

    async def extend_volume(
        self, usage_uuid: str, volume_id: int, volume_size: int
    ) -> None:
        """Extend the volume of the application."""
        print(
            f"Extending volume of app {await self.get_app_name(usage_uuid)} to {volume_size}GB"
        )

        try:
            # get the volume id
            response = await run_async_command(
                [
                    "fly",
                    "volume",
                    "list",
                    "-a",
                    await self.get_app_name(usage_uuid),
                    "--json",
                ],
                print_output=False,
            )

            volumes = json.loads(response.stdout)
            for volume in volumes:
                if volume["name"] == await self.get_app_volume_name(volume_id):
                    volume_id = volume["id"]
                    break

            if not volume_id:
                raise ValueError(f"Volume {volume_id} not found")

        except subprocess.CalledProcessError as e:
            raise e

        try:
            await run_async_command(
                [
                    "fly",
                    "volume",
                    "extend",
                    str(volume_id),
                    "-s",
                    str(volume_size),
                    "-a",
                    await self.get_app_name(usage_uuid),
                ]
            )

        except subprocess.CalledProcessError as e:
            raise e

    async def get_check_status(self, usage_uuid: str, machine_id: int) -> CheckStatus:
        """Get the status of the check for the application."""
        app_name = await self.get_app_name(usage_uuid)
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

    async def clean(self, usage_uuid: str) -> None:
        """
        Cleanup the app on fly.
        If there are no machines, we will remove the allocated IP address.
        If there are machines, and no allocated IP address, we will allocate one.
        If there are machines, and an allocated IP address, we will do nothing.
        """
        machines = await db.machines.afind(filters={"usage_uuid": usage_uuid})
        volumes = await db.file_systems.afind(filters={"usage_uuid": usage_uuid})
        ip_address = await self._get_allocated_ip_address(usage_uuid)

        app_name = await self.get_app_name(usage_uuid)

        if len(machines) == 0 and len(volumes) == 0:
            await self.destroy_app(usage_uuid)

        elif len(machines) == 0 and ip_address:
            await self.release_ip_address(usage_uuid)

        elif len(machines) > 0 and not ip_address:
            await self._allocate_ip_address(usage_uuid)
