from typing import Optional
from tenacity import retry, stop_after_attempt, wait_exponential
from loguru import logger
from services.fly.api.base import BaseFlyAPI
from services.fly.utils import get_fly_volume_id, run_async_command


class VolumesAPI(BaseFlyAPI):
    def __init__(self):
        super().__init__()

    @retry(
        stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=15)
    )
    async def list(self, app_name: str) -> list[dict]:
        url = f"/{app_name}/volumes"
        response = await self._get(url)
        return response

    @retry(
        stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=15)
    )
    async def get(self, app_name: str, volume_name: str) -> dict | None:
        volume_id = await get_fly_volume_id(app_name, volume_name=volume_name)
        url = f"/{app_name}/volumes/{volume_id}"
        response = await self._get(url)
        return response

    @retry(
        stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=15)
    )
    async def create(
        self,
        volume_name: str,
        app_name: str,
        region: str,
        size: int,
        gpu_kind: Optional[str] = None,
    ) -> None:
        url = f"/{app_name}/volumes"

        compute_data = {
            "cpu_kind": "performance",
            "cpus": 16, # just say lots of cpu so we reserve spots on server
            "gpu_kind": gpu_kind,
            "gpus": 1 if gpu_kind is not None else None,
            "host_dedication_id": "",
            "kernel_args": [""],
            "memory_mb": 131072,  # 128GB, just say lots of memory so we reserve spots on server
        }

        data = {
            "name": volume_name,
            "region": region,
            "size_gb": size,
            "compute": compute_data,
        }

        logger.info(
            f"Creating volume {volume_name} on {region} with {size}GB and {gpu_kind} GPU"
        )

        response = await self._post(url, json=data)
        return response

    @retry(
        stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=15)
    )
    async def extend(self, app_name: str, volume_id: str, size: int) -> None:
        url = f"/{app_name}/volumes/{volume_id}/extend"
        data = {"size_gb": size}
        await self._put(url, json=data)

    @retry(
        stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=15)
    )
    async def destroy(self, app_name: str, volume_id: str) -> None:
        url = f"/{app_name}/volumes/{volume_id}"
        await self._delete(url)

    async def fork(self, app_name: str, volume_id: str, new_volume_name: str) -> None:
        await run_async_command(
            [
                "fly",
                "volumes",
                "fork",
                volume_id,
                "--name",
                new_volume_name,
                "--app",
                app_name,
            ]
        )
