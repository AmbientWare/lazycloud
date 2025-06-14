import asyncio
import time
from pathlib import Path
from loguru import logger
from typing import Optional
from lazycloud_api.config import app_config
from lazycloud_api.services.fly.schemas import (
    FlyMachineConfig,
    RESOURCE_MAP,
)
from lazycloud_api.services.fly.utils import (
    get_app_name,
    get_machine_name,
    get_app_volume_name,
    get_fly_volume_id,
)
from lazycloud_api.database.machines import MachinePydantic
from lazycloud_api.database import db
from lazycloud_api.database.machines import MachineStatus
from lazycloud_api.services.fly.schemas import FlyRegion
from lazycloud_api.services.fly.api import fly_api
from lazycloud_api.database.volumes import VolumePydantic, VolumeTypes
from lazycloud_api.services.platform.schemas import ImageTypes


class FlyAppManager:
    def __init__(self, org_name: str = app_config.FLY_ORG_NAME):
        """Initialize the Fly.io app manager.

        Args:
            org_name: The Fly.io organization name
            base_dir: The base directory for app files. Defaults to the directory containing this file.
        """
        self.org_name = org_name
        self.base_dir = Path(__file__).resolve().parent

    async def _check_app_exists(self, usage_uuid: str, machine_id: int):
        app_name = await get_app_name(usage_uuid, machine_id)
        app = await fly_api.apps.get(app_name)
        return app is not None

    # create the app on fly
    async def _ensure_app_exists(self, usage_uuid: str, machine_id: int) -> None:
        if not await self._check_app_exists(usage_uuid, machine_id):
            logger.info(f"No app found for usage_uuid: {usage_uuid}, Creating app.")
            await self.create_app(usage_uuid, machine_id)

        else:
            logger.info(f"App found for usage_uuid: {usage_uuid}. Skipping creation.")

    async def _get_machine_id(self, usage_uuid: str, machine_id: int) -> str | None:
        """Get the machine id for the application."""
        app_name = await get_app_name(usage_uuid, machine_id)
        machines = await fly_api.machines.list(app_name)
        machine_name = await get_machine_name(machine_id)

        for machine in machines:
            if machine.get("name") == machine_name:
                return machine.get("id")

        logger.error(f"Machine {machine_name} not found on fly.")
        return None

    async def get_volume_info(
        self, usage_uuid: str, machine_id: int, volume_id: int
    ) -> tuple[int | None, str | None]:
        """Get the volume size of the Fly.io application."""
        app_name = await get_app_name(usage_uuid, machine_id)
        volume = await fly_api.volumes.get(app_name, str(volume_id))

        if volume:
            return volume.get("size_gb"), volume.get("region")

        return None, None

    async def create_app(self, usage_uuid: str, machine_id: int) -> None:
        """Create a new Fly.io application."""
        logger.info(f"Creating app {await get_app_name(usage_uuid, machine_id)}")
        # Create the app and allocate an IP address
        await fly_api.apps.create(await get_app_name(usage_uuid, machine_id))
        await fly_api.apps._allocate_ip_address(usage_uuid, machine_id)

    async def destroy_app(self, usage_uuid: str, machine_id: int) -> None:
        """Delete a Fly.io application."""
        logger.info(f"Deleting app {await get_app_name(usage_uuid, machine_id)}")
        try:
            await fly_api.apps.destroy(await get_app_name(usage_uuid, machine_id))
        except Exception as e:
            logger.error(f"Error destroying app: {e}")

        # delete machine from db. this will cascade delete any associated volumes.
        await db.machines.adelete(machine_id)

    async def create_volume(
        self,
        usage_uuid: str,
        volume_id: int,
        machine_id: int,
        size: int,
        region: FlyRegion,
        gpu_kind: Optional[str] = None,
    ) -> str:
        """Create a volume for the application."""
        # first make sure the app exists, if not create it
        await self._ensure_app_exists(usage_uuid, machine_id)

        volume_name = await get_app_volume_name(volume_id)
        logger.info(f"Creating volume {volume_name} for {volume_id}")
        await fly_api.volumes.create(
            volume_name,
            await get_app_name(usage_uuid, machine_id),
            region.value,
            size,
            gpu_kind,
        )

        return volume_name

    async def destroy_volume(
        self, usage_uuid: str, machine_id: int, volume_id: int
    ) -> None:
        """Destroy a volume for the application."""
        logger.info(f"Destroying volume for {volume_id}")
        try:
            fly_volume_id = await get_fly_volume_id(usage_uuid, volume_id)
            if fly_volume_id:
                await fly_api.volumes.destroy(
                    await get_app_name(usage_uuid, machine_id), fly_volume_id
                )
        except Exception as e:
            logger.error(f"Error destroying volume {volume_id}: {e}")

        try:
            await db.volumes.adelete(volume_id)

        except Exception as e:
            logger.error(f"Error destroying volume {volume_id}: {e}")
            raise e

    async def create_machine(
        self,
        user_id: str,
        name: str,
        machine_config: FlyMachineConfig,
    ) -> MachinePydantic:
        """Deploy the application to Fly.io."""

        # First create a machine in our database
        new_machine = await db.machines.acreate(
            MachinePydantic(
                user_id=user_id,
                name=name,
                region=machine_config.region.value,
                image=ImageTypes.UBUNTU_22_04.value,
                cpu_kind=machine_config.cpu_kind,
                cpu=machine_config.cpu,
                memory=machine_config.memory,
                gpu_kind=machine_config.gpu_kind,
                app_port=22,
                status=MachineStatus.INITIALIZING,
            )
        )

        if new_machine.id is None:
            raise ValueError("Failed to create machine")

        # Set the machine_id in the config now that we have it
        machine_config.machine_id = new_machine.id

        # Next create a volume in our database
        new_volume = await db.volumes.acreate(
            VolumePydantic(
                user_id=user_id,
                machine_id=new_machine.id,
                name=name,
                size=machine_config.volume_size,
                region=machine_config.region.value,
                type=VolumeTypes.ROOT,
                mount_path="/data",  # NOTE: /data is eventually mounted as root on the machine
            )
        )

        if new_volume.id is None:
            raise ValueError("Failed to create volume")

        logger.info(
            f"Deploying app {await get_app_name(machine_config.usage_uuid, new_machine.id)}"
        )

        try:
            await self._ensure_app_exists(machine_config.usage_uuid, new_machine.id)

            await db.machines.update_machine_status(
                new_machine.id, MachineStatus.VM_CREATING
            )

            # create a volume in fly
            await self.create_volume(
                usage_uuid=machine_config.usage_uuid,
                volume_id=new_volume.id,
                machine_id=new_machine.id,
                size=machine_config.volume_size,
                region=machine_config.region,
            )

            await fly_api.machines.create(
                app_name=await get_app_name(machine_config.usage_uuid, new_machine.id),
                volume_id=new_volume.id,
                machine_config=machine_config,
            )

            await db.machines.update_machine_status(
                new_machine.id, MachineStatus.BUILDING
            )

            # slight sleep to ensure the machine is created
            await asyncio.sleep(5)

            await self.wait_for_checks(machine_config.usage_uuid, new_machine.id)

        except Exception as e:
            logger.error(f"Error creating machine: {e}")
            # delete fly app
            await self.destroy_app(machine_config.usage_uuid, new_machine.id)
            # delete machine from db. this will cascade delete any associated volumes.
            await db.machines.adelete(new_machine.id)
            raise e

        await db.machines.update_machine_status(new_machine.id, MachineStatus.DEPLOYED)

        return new_machine

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
                app_name=await get_app_name(usage_uuid, machine_id),
                machine_id=fly_machine_id,
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
            valid_memory_gb = [
                f"{opt / 1024:.1f}" for opt in vm_config["memory_options"]
            ]
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
            app_name=await get_app_name(usage_uuid, machine_id),
            machine_id=fly_machine_id,
            cpu_kind=cpu_kind,
            cpus=cpu,
            memory=memory,
        )

        # wait for the machine to be updated
        await self.wait_for_checks(usage_uuid, machine_id)

    async def restart_machine(self, usage_uuid: str, machine_id: int) -> None:
        """Restart the machine."""
        fly_machine_id = await self._get_machine_id(usage_uuid, machine_id)
        if not fly_machine_id:
            raise ValueError(
                f"Machine {await get_machine_name(machine_id)} not found on fly."
            )

        await fly_api.machines.restart(
            app_name=await get_app_name(usage_uuid, machine_id),
            machine_id=fly_machine_id,
        )

        # wait for the machine to be updated
        await self.wait_for_checks(usage_uuid, machine_id)

    async def auto_stop(self, usage_uuid: str, machine_id: int, enabled: bool) -> None:
        """Enable or disable auto stop for the machine."""
        fly_machine_id = await self._get_machine_id(usage_uuid, machine_id)
        if not fly_machine_id:
            raise ValueError(
                f"Machine {await get_machine_name(machine_id)} not found on fly."
            )

        await fly_api.machines.auto_stop(
            app_name=await get_app_name(usage_uuid, machine_id),
            machine_id=fly_machine_id,
            enabled=enabled,
        )

        # wait for the machine to be updated
        await self.wait_for_checks(usage_uuid, machine_id)

    async def extend_volume(
        self, usage_uuid: str, machine_id: int, volume_id: int, volume_size: int
    ) -> None:
        """Extend the volume of the application."""
        print(
            f"Extending volume of app {await get_app_name(usage_uuid, machine_id)} to {volume_size}GB"
        )

        fly_volume_id = await get_fly_volume_id(usage_uuid, machine_id, volume_id)
        if fly_volume_id is None:
            raise ValueError(f"Volume {volume_id} not found on fly.")

        await fly_api.volumes.extend(
            app_name=await get_app_name(usage_uuid, machine_id),
            volume_id=fly_volume_id,
            size=volume_size,
        )

    async def wait_for_checks(
        self, usage_uuid: str, machine_id: int, timeout: int = 120
    ):
        """Get the status of the check for the application."""
        app_name = await get_app_name(usage_uuid, machine_id)
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
