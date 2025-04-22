import asyncio
import time
from pathlib import Path
from loguru import logger

from machines.config import app_config
from machines.services.fly.schemas import (
    FlyMachineConfig,
    RESOURCE_MAP,
)
from machines.services.fly.utils import (
    get_app_name,
    get_machine_name,
    get_app_volume_name,
    get_fly_volume_id,
)
from machines.database import db
from machines.database.machines import MachineStatus
from machines.services.fly.schemas import FlyRegion
from machines.services.fly.api import fly_api
from machines.database.file_systems import FileSystemPydantic


class FlyAppManager:
    def __init__(self, org_name: str = app_config.FLY_ORG_NAME):
        """Initialize the Fly.io app manager.

        Args:
            org_name: The Fly.io organization name
            base_dir: The base directory for app files. Defaults to the directory containing this file.
        """
        self.org_name = org_name
        self.base_dir = Path(__file__).resolve().parent

    # create the app on fly
    async def _ensure_app_exists(self, usage_uuid: str) -> None:
        if not await self.check_app_exists(usage_uuid):
            logger.info(f"No app found for usage_uuid: {usage_uuid}, Creating app.")
            await self.create_app(usage_uuid)

        else:
            logger.info(f"App found for usage_uuid: {usage_uuid}. Skipping creation.")

    async def _get_machine_id(self, usage_uuid: str, machine_id: int) -> str | None:
        """Get the machine id for the application."""
        app_name = await get_app_name(usage_uuid)
        machines = await fly_api.machines.list(app_name)
        machine_name = await get_machine_name(machine_id)

        for machine in machines:
            if machine.get("name") == machine_name:
                return machine.get("id")

        logger.error(f"Machine {machine_name} not found on fly.")
        return None

    async def check_app_exists(self, usage_uuid: str):
        app_name = await get_app_name(usage_uuid)
        app = await fly_api.apps.get(app_name)
        return app is not None

    async def get_volume_info(
        self, usage_uuid: str, volume_id: int
    ) -> tuple[int | None, str | None]:
        """Get the volume size of the Fly.io application."""
        app_name = await get_app_name(usage_uuid)
        volume = await fly_api.volumes.get(app_name, str(volume_id))

        if volume:
            return volume.get("size_gb"), volume.get("region")

        return None, None

    async def create_app(self, usage_uuid: str) -> None:
        """Create a new Fly.io application."""
        logger.info(f"Creating app {await get_app_name(usage_uuid)}")
        # Create the app and allocate an IP address
        await fly_api.apps.create(await get_app_name(usage_uuid))
        await fly_api.apps._allocate_ip_address(usage_uuid)

    async def destroy_app(self, usage_uuid: str) -> None:
        """Delete a Fly.io application."""
        logger.info(f"Deleting app {await get_app_name(usage_uuid)}")
        # also release the IP address as this also deletes the CNAME record
        await fly_api.apps.release_ip_address(usage_uuid)
        await fly_api.apps.destroy(await get_app_name(usage_uuid))

    async def create_file_system(
        self,
        usage_uuid: str,
        file_system_id: int,
        size: int,
        region: FlyRegion,
    ) -> str:
        """Create a file system for the application."""
        # first make sure the app exists, if not create it
        await self._ensure_app_exists(usage_uuid)

        volume_name = await get_app_volume_name(file_system_id)
        logger.info(f"Creating volume {volume_name} for {file_system_id}")
        await fly_api.volumes.create(
            volume_name, await get_app_name(usage_uuid), region.value, size
        )

        return volume_name

    async def destroy_file_system(self, usage_uuid: str, file_system_id: int) -> None:
        """Destroy a file system for the application."""
        logger.info(f"Destroying file system for {file_system_id}")
        try:
            fly_volume_id = await get_fly_volume_id(usage_uuid, file_system_id)
            if fly_volume_id:
                await fly_api.volumes.destroy(
                    await get_app_name(usage_uuid), fly_volume_id
                )
        except Exception as e:
            logger.error(f"Error destroying file system {file_system_id}: {e}")

        try:
            await db.file_systems.adelete(file_system_id)
            # finally clean up the app
            await self.clean(usage_uuid)
        except Exception as e:
            logger.error(f"Error destroying file system {file_system_id}: {e}")
            raise e

    async def duplicate_file_system(
        self,
        usage_uuid: str,
        file_system: FileSystemPydantic,
        new_file_system_name: str,
    ) -> FileSystemPydantic:
        """Duplicate a file system for the application."""
        if not file_system or file_system.id is None:
            raise ValueError(f"File system {file_system.id} not found")

        logger.info(f"Duplicating file system for {file_system.id}")
        current_volume_id = await get_fly_volume_id(usage_uuid, file_system.id)
        if current_volume_id is None:
            raise ValueError(f"File system {file_system.id} not found on fly.")

        # now create a new file system in the database
        new_file_system = await db.file_systems.acreate(
            FileSystemPydantic(
                user_id=file_system.user_id,
                name=new_file_system_name,
                size=file_system.size,
                region=file_system.region,
            )
        )

        if new_file_system.id is None or new_file_system.id == file_system.id:
            raise ValueError(f"Failed to create new file system")

        try:
            new_fly_volume_name = await get_app_volume_name(new_file_system.id)
            await fly_api.volumes.fork(
                await get_app_name(usage_uuid),
                current_volume_id,
                new_fly_volume_name,
            )

        except Exception as e:
            logger.error(f"Error duplicating file system {file_system.id}: {e}")
            await db.file_systems.adelete(new_file_system.id)
            raise e

        return new_file_system

    async def create_machine(
        self,
        machine_config: FlyMachineConfig,
    ) -> None:
        """Deploy the application to Fly.io."""
        # first make sure the app exists, if not create it
        await self._ensure_app_exists(machine_config.usage_uuid)

        logger.info(f"Deploying app {await get_app_name(machine_config.usage_uuid)}")

        await db.machines.update_machine_status(
            machine_config.machine_id, MachineStatus.BUILDING
        )

        await fly_api.machines.create(
            app_name=await get_app_name(machine_config.usage_uuid),
            machine_config=machine_config,
        )

        try:
            await db.machines.update_machine_status(
                machine_config.machine_id, MachineStatus.VM_CREATING
            )

            # slight sleep to ensure the machine is created
            await asyncio.sleep(5)

            await self.wait_for_checks(
                machine_config.usage_uuid, machine_config.machine_id
            )

        finally:
            await db.machines.update_machine_status(
                machine_config.machine_id, MachineStatus.DEPLOYED
            )

            await self.clean(machine_config.usage_uuid)

    async def destroy_machine(self, usage_uuid: str, machine_id: int) -> None:
        """Destroy the machine."""
        machine_name = await get_machine_name(machine_id)
        logger.info(f"Destroying machine {machine_name}")
        await db.machines.update_machine_status(machine_id, MachineStatus.DELETING)
        # get the machine id
        fly_machine_id = await self._get_machine_id(usage_uuid, machine_id)
        if fly_machine_id:
            logger.info(f"Destroying machine {fly_machine_id} in fly")
            await fly_api.machines.destroy(
                app_name=await get_app_name(usage_uuid), machine_id=fly_machine_id
            )

        else:
            logger.warning(f"Machine {machine_name} not found on fly.")

        # delete the machine from the database
        logger.info(f"Deleting machine {machine_name} from database")
        await db.machines.adelete(machine_id)

    async def scale_machine(
        self, usage_uuid: str, machine_id: int, cpu_kind: str, cpu: int, memory: int
    ) -> None:
        """Scale the application to the given number of machines."""
        logger.info(
            f"Scaling machine {await get_machine_name(machine_id)} to {cpu_kind} {cpu} {memory}"
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

        if not fly_machine_id:
            raise ValueError(
                f"Machine {await get_machine_name(machine_id)} not found on fly."
            )

        await fly_api.machines.update(
            app_name=await get_app_name(usage_uuid),
            machine_id=fly_machine_id,
            cpu_kind=cpu_kind,
            cpus=cpu,
            memory=memory,
        )

        # wait for the machine to be updated
        await self.wait_for_checks(usage_uuid, machine_id)

    async def extend_file_system(
        self, usage_uuid: str, file_system_id: int, volume_size: int
    ) -> None:
        """Extend the volume of the application."""
        print(
            f"Extending volume of app {await get_app_name(usage_uuid)} to {volume_size}GB"
        )

        volume_id = await get_fly_volume_id(usage_uuid, file_system_id)
        if volume_id is None:
            raise ValueError(f"File system {file_system_id} not found on fly.")

        await fly_api.volumes.extend(
            app_name=await get_app_name(usage_uuid),
            volume_id=volume_id,
            size=volume_size,
        )

    async def wait_for_checks(
        self, usage_uuid: str, machine_id: int, timeout: int = 120
    ):
        """Get the status of the check for the application."""
        app_name = await get_app_name(usage_uuid)
        fly_machine_id = await self._get_machine_id(usage_uuid, machine_id)
        if not fly_machine_id:
            logger.error(f"Machine {machine_id} not found on fly.")
            return

        start_time = time.time()
        while time.time() - start_time < timeout:
            machine = await fly_api.machines.get(app_name, fly_machine_id)

            if not machine:
                raise Exception(f"Machine {fly_machine_id} not found on fly.")

            checks = machine.get("checks", {})
            passing_list = [False] * len(checks)
            for loc, check in enumerate(checks):
                if check.get("status") == "passing":
                    passing_list[loc] = True

            if all(passing_list):
                return

            await asyncio.sleep(2)

        raise Exception(
            f"Machine {await get_machine_name(machine_id)} failed to deploy"
        )

    async def clean(self, usage_uuid: str) -> None:
        """
        Cleanup the app on fly.
        If there are no machines, we will remove the allocated IP address.
        If there are machines, and no allocated IP address, we will allocate one.
        If there are machines, and an allocated IP address, we will do nothing.
        """
        machines = await db.machines.afind(filters={"usage_uuid": usage_uuid})
        volumes = await db.file_systems.afind(filters={"usage_uuid": usage_uuid})
        ip_address = await fly_api.apps.get_allocated_ip_address(usage_uuid)

        # get volumes from fly, we will destroy any volumes that are not in the database
        fly_volumes = await fly_api.volumes.list(await get_app_name(usage_uuid))
        db_volume_names = [
            await get_app_volume_name(v.id) for v in volumes if v.id is not None
        ]
        volumes_to_destroy_promises = []
        for fly_volume in fly_volumes:
            if (
                fly_volume.get("name") not in db_volume_names
                and fly_volume.get("status") != "pending_destroy"
            ):
                volumes_to_destroy_promises.append(
                    fly_api.volumes.destroy(
                        await get_app_name(usage_uuid), fly_volume["id"]
                    )
                )

        await asyncio.gather(*volumes_to_destroy_promises)

        logger.info(
            f"cleaning up app {await get_app_name(usage_uuid)}: {len(machines)} machines, {len(volumes)} volumes, {ip_address} ip address"
        )

        if len(machines) == 0 and len(volumes) == 0:
            await self.destroy_app(usage_uuid)

        elif len(machines) == 0 and ip_address:
            await fly_api.apps.release_ip_address(usage_uuid)

        elif len(machines) > 0 and not ip_address:
            await fly_api.apps._allocate_ip_address(usage_uuid)
