from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_exponential
from loguru import logger

from machines.services.fly.api.base import BaseFlyAPI
from machines.services.fly.schemas import FlyMachineConfig
from machines.services.platform.schemas import IMAGE_MAP
from machines.services.fly.utils import (
    get_machine_name,
    get_fly_volume_id,
)


class CreateMachineModel(BaseModel):
    name: str
    region: str
    size: str
    image: str


class MachinesAPI(BaseFlyAPI):
    def __init__(self):
        super().__init__()

    @retry(
        stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=15)
    )
    async def list(self, app_name: str) -> list[dict]:
        url = f"/{app_name}/machines"
        response = await self._get(url)
        return response

    @retry(
        stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=15)
    )
    async def get(self, app_name: str, machine_id: str) -> dict | None:
        url = f"/{app_name}/machines/{machine_id}"
        response = await self._get(url)
        return response

    @retry(
        stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=15)
    )
    async def wait_for_status(
        self, app_name: str, machine_id: str, status: str = "started"
    ) -> None:
        url = f"/{app_name}/machines/{machine_id}/wait"
        query_params = {"status": status}
        await self._post(url, params=query_params)

    @retry(
        stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=15)
    )
    async def create(self, app_name: str, machine_config: FlyMachineConfig) -> None:
        machine_name = await get_machine_name(machine_config.machine_id)
        volume_id = await get_fly_volume_id(
            machine_config.usage_uuid, machine_config.file_system_id
        )

        url = f"/{app_name}/machines"
        data = {
            "name": machine_name,
            "region": machine_config.region.value,
            "config": {
                "env": {
                    "AUTHORIZED_KEYS": machine_config.public_key,
                },
                "guest": {
                    "cpu_kind": machine_config.cpu_kind,
                    "cpus": machine_config.cpu,
                    "memory_mb": machine_config.memory,
                },
                "image": IMAGE_MAP[machine_config.image_type],
                "services": [
                    {
                        "ports": [
                            {"port": machine_config.port},
                        ],
                        "protocol": "tcp",
                        "internal_port": 2222,
                        "autostart": True,
                        "autostop": "suspend",
                    }
                ],
                "checks": {
                    "ssh_check": {
                        "grace_period": "40s",
                        "type": "tcp",
                        "port": 2222,
                        "interval": "30s",
                        "timeout": "10s",
                    }
                },
                "mounts": [
                    {
                        "volume": volume_id,
                        "path": "/data",
                        "extend_threshold_percent": 90,
                        "add_size_gb": 10,
                        "size_gb_limit": 500,
                    }
                ],
            },
        }

        # add a gpu if machine_config.gpu_kind is not None
        if machine_config.gpu_kind is not None:
            data["config"]["guest"]["gpu_kind"] = machine_config.gpu_kind
            # only allocate 1 gpu TODO: maybe make this configurable in the future?
            data["config"]["guest"]["gpus"] = 1

        response = await self._post(url, json=data)

        await self.wait_for_status(app_name, response.get("id"))

        return response

    @retry(
        stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=15)
    )
    async def update(
        self, app_name: str, machine_id: str, cpu_kind: str, cpus: int, memory: int
    ) -> None:
        url = f"/{app_name}/machines/{machine_id}"

        # get the current machine config
        machine = await self.get(app_name, machine_id)
        if machine is None:
            raise ValueError(f"Machine {machine_id} not found")

        # update the machine config
        machine["config"]["guest"]["cpu_kind"] = cpu_kind
        machine["config"]["guest"]["cpus"] = cpus
        machine["config"]["guest"]["memory_mb"] = memory

        await self._post(url, json=machine)

    @retry(
        stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=15)
    )
    async def restart(self, app_name: str, machine_id: str) -> None:
        url = f"/{app_name}/machines/{machine_id}/restart"

        # get the current machine config
        machine = await self.get(app_name, machine_id)
        if machine is None:
            raise ValueError(f"Machine {machine_id} not found")

        await self._post(url, json=machine)

    @retry(
        stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=15)
    )
    async def auto_stop(self, app_name: str, machine_id: str, enabled: bool) -> None:
        url = f"/{app_name}/machines/{machine_id}"

        # get the current machine config
        machine = await self.get(app_name, machine_id)
        if machine is None:
            raise ValueError(f"Machine {machine_id} not found")

        # update the machine config
        machine["config"]["autostop"] = "suspend" if enabled else None

        await self._post(url, json=machine)

    @retry(
        stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=15)
    )
    async def destroy(self, app_name: str, machine_id: str) -> None:
        url = f"/{app_name}/machines/{machine_id}"
        query_params = {"force": True}
        await self._delete(url, params=query_params)
