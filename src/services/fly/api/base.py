from typing import Dict, Optional, Any
import httpx
from loguru import logger

from src.config import app_config


class BaseFlyAPI:
    def __init__(self):
        self._base_url = "https://api.machines.dev/v1/apps"
        self.timeout = 120.0

    def _get_client(self) -> httpx.AsyncClient:
        """Get an HTTP client with authentication"""
        headers = {}
        if app_config.FLY_API_TOKEN:
            headers["Authorization"] = f"Bearer {app_config.FLY_API_TOKEN}"
        else:
            raise Exception(
                "No API key set. Please set an API key with `lazycloud keys add`"
            )

        headers["Content-Type"] = "application/json"

        return httpx.AsyncClient(timeout=self.timeout, headers=headers)

    def _finalize_url(self, path: str) -> str:
        return f"{self._base_url}{path}" if path else self._base_url

    async def _make_request(
        self,
        method: str,
        url: str,
        json: Optional[Dict[str, Any]] = None,
        params: Optional[dict] = None,
    ) -> Any:
        """Make a request to the API"""
        client = self._get_client()
        try:
            async with client:
                logger.info(f"Making request to {url} with method {method}")
                response = await client.request(method, url, json=json, params=params)
                response.raise_for_status()

                # For 202 Accepted and 204 No Content, return None
                if response.status_code in (202, 204):
                    return None

                # Check if there's actually content to parse
                content_type = response.headers.get("content-type", "")
                if not response.content or not content_type.startswith(
                    "application/json"
                ):
                    return None

                return response.json()

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None

            logger.error(f"HTTP error occurred: {str(e)}")
            logger.error(
                f"Response body: {e.response.text if e.response else 'No response body'}"
            )
            # try to parse the response body as json
            try:
                response_json = e.response.json()
                if "error" in response_json:
                    logger.error(f"Error response: {response_json['error']}")
                    raise Exception(response_json["error"])
            except Exception:
                pass

            raise e

        except Exception as e:
            logger.error(
                f"Error making request to {url} with method {method}: {str(e)}"
            )
            raise e

    async def _get(
        self,
        path: str = "",
        params: Optional[dict] = None,
        json: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Get a resource from the API"""
        url = self._finalize_url(path)
        return await self._make_request("GET", url, json, params)

    async def _post(
        self,
        path: str = "",
        params: Optional[dict] = None,
        json: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Post a resource to the API"""
        url = self._finalize_url(path)
        return await self._make_request("POST", url, json, params)

    async def _put(
        self,
        path: str = "",
        params: Optional[dict] = None,
        json: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Put a resource to the API"""
        url = self._finalize_url(path)
        return await self._make_request("PUT", url, json, params)

    async def _delete(
        self,
        path: str = "",
        params: Optional[dict] = None,
        json: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Delete a resource from the API"""
        url = self._finalize_url(path)
        return await self._make_request("DELETE", url, json, params)
