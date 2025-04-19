from tenacity import retry, stop_after_attempt, wait_exponential

from machines.services.fly.api.base import BaseFlyAPI
from machines.config import app_config


class AppsAPI(BaseFlyAPI):
    def __init__(self):
        super().__init__()

    @retry(
        stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=15)
    )
    async def list(self) -> list[dict]:
        params = {"org_slug": app_config.FLY_ORG_NAME}
        response = await self._get(params=params)

        return response.get("apps")

    @retry(
        stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=15)
    )
    async def get(self, app_name: str) -> dict | None:
        url = f"/{app_name}"
        response = await self._get(url)
        return response

    @retry(
        stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=15)
    )
    async def create(self, app_name: str) -> None:
        data = {
            "app_name": app_name,
            "org_slug": app_config.FLY_ORG_NAME,
            "network": f"lc-network-{app_name}",
        }

        response = await self._post(json=data)
        return response

    @retry(
        stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=15)
    )
    async def destroy(self, app_name: str) -> None:
        url = f"/{app_name}"
        await self._delete(url)
