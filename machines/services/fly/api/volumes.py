from tenacity import retry, stop_after_attempt, wait_exponential
from loguru import logger

from machines.services.fly.api.base import BaseFlyAPI
from machines.services.fly.utils import get_volume_id


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
        volume_id = await get_volume_id(app_name, volume_name=volume_name)
        url = f"/{app_name}/volumes/{volume_id}"
        response = await self._get(url)
        return response

    @retry(
        stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=15)
    )
    async def create(
        self, volume_name: str, app_name: str, region: str, size: int
    ) -> None:
        url = f"/{app_name}/volumes"
        data = {
            "name": volume_name,
            "region": region,
            "size_gb": size,
        }
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
