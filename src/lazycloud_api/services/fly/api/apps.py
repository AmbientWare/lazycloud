from tenacity import retry, stop_after_attempt, wait_exponential
import json

from lazycloud_api.services.fly.api.base import BaseFlyAPI
from lazycloud_api.config import app_config
from lazycloud_api.services.fly.utils import run_async_command, get_app_name

# NOTE: anything useing the fly cli is not currently available in the fly api


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

    async def _allocate_ip_address(self, usage_uuid: str) -> None:
        """Allocate an IP address for the application."""
        app_name = await get_app_name(usage_uuid)
        await run_async_command(
            ["fly", "ips", "allocate-v4", "--app", app_name, "--yes"]
        )

    async def get_allocated_ip_address(self, usage_uuid: str) -> str | None:
        """Get the allocated IP address for the application."""
        app_name = await get_app_name(usage_uuid)
        response = await run_async_command(
            ["fly", "ips", "list", "--app", app_name, "--json"],
            print_output=False,
        )
        ips = json.loads(response.stdout)
        if len(ips) > 0:
            return ips[0].get("Address")

        return None

    async def release_ip_address(self, usage_uuid: str) -> None:
        """Release an IP address for the application."""
        app_name = await get_app_name(usage_uuid)
        ip_address = await self.get_allocated_ip_address(usage_uuid)
        if ip_address:
            await run_async_command(
                ["fly", "ips", "release", ip_address, "--app", app_name]
            )
