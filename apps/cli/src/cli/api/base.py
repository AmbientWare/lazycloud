from typing import Any, Callable

import httpx

from cli.api.utils import Spinner, StatusSpinner
from cli.config import config


class APIError(Exception):
    """Custom exception that preserves HTTP status code from API errors."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class BaseAPI:
    def __init__(self, url_path: str, use_version: bool = True):
        if use_version:
            self._base_url = f"{config.api_base_url}/{config.api_version}/{url_path}"
        else:
            self._base_url = f"{config.api_base_url}/{url_path}"
        self.timeout = 300.0

    def _get_headers(self) -> dict[str, str]:
        """Get authentication headers"""
        headers = {}
        api_key = config.api_key
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def _get_client(self) -> httpx.Client:
        """Get a synchronous HTTP client with authentication"""
        return httpx.Client(timeout=self.timeout, headers=self._get_headers())

    def _get_async_client(self) -> httpx.AsyncClient:
        """Get an async HTTP client with authentication"""
        return httpx.AsyncClient(timeout=self.timeout, headers=self._get_headers())

    def _make_request(
        self,
        method: str,
        url: str,
        json: dict[str, Any] | None = None,
        params: dict | None = None,
    ) -> Any:
        """Make a request to the API"""
        client = self._get_client()
        try:
            response = client.request(method, url, json=json, params=params)
            response.raise_for_status()
            return response.json()

        except httpx.HTTPStatusError as e:
            # Try to get error message from response JSON
            try:
                error_data = e.response.json()
                if isinstance(error_data, dict):
                    if "detail" in error_data:
                        error_message = error_data["detail"]
                    else:
                        error_message = (
                            error_data.get("message")
                            or error_data.get("error")
                            or str(error_data)
                        )
                else:
                    error_message = str(error_data)
            except Exception:
                # If can't parse JSON, use status code and reason
                error_message = (
                    f"HTTP {e.response.status_code}: {e.response.reason_phrase}"
                )
            raise APIError(error_message, status_code=e.response.status_code) from e

        except Exception as e:
            raise APIError(str(e)) from e

    async def _make_request_async(
        self,
        method: str,
        url: str,
        json: dict[str, Any] | None = None,
        params: dict | None = None,
    ) -> Any:
        """Make an async request to the API"""
        async with self._get_async_client() as client:
            try:
                response = await client.request(method, url, json=json, params=params)
                response.raise_for_status()
                return response.json()

            except httpx.HTTPStatusError as e:
                # Try to get error message from response JSON
                try:
                    error_data = e.response.json()
                    if isinstance(error_data, dict):
                        if "detail" in error_data:
                            error_message = error_data["detail"]
                        else:
                            error_message = (
                                error_data.get("message")
                                or error_data.get("error")
                                or str(error_data)
                            )
                    else:
                        error_message = str(error_data)

                except Exception:
                    # If can't parse JSON, use status code and reason
                    error_message = (
                        f"HTTP {e.response.status_code}: {e.response.reason_phrase}"
                    )

                raise APIError(error_message, status_code=e.response.status_code) from e

            except Exception as e:
                raise APIError(str(e)) from e

    def _get(
        self,
        path: str = "",
        params: dict | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        """Get a resource from the API"""
        url = self._base_url if not path else f"{self._base_url}{path}"
        return self._make_request("GET", url, json, params)

    def _post(
        self,
        path: str = "",
        params: dict | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        """Post a resource to the API"""
        url = self._base_url if not path else f"{self._base_url}{path}"
        return self._make_request("POST", url, json, params)

    def _patch(
        self,
        path: str = "",
        params: dict | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        """Patch a resource to the API"""
        url = self._base_url if not path else f"{self._base_url}{path}"
        return self._make_request("PATCH", url, json, params)

    def _put(
        self,
        path: str = "",
        params: dict | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        """Put a resource to the API"""
        url = self._base_url if not path else f"{self._base_url}{path}"
        return self._make_request("PUT", url, json, params)

    def _delete(
        self,
        path: str = "",
        params: dict | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        """Delete a resource from the API"""
        url = self._base_url if not path else f"{self._base_url}{path}"
        return self._make_request("DELETE", url, json, params)

    def _run_with_spinner(
        self,
        message: str,
        func: Callable,
        status_checker: Callable[[], str] | None = None,
    ) -> Any:
        """Run a function with a spinner in a separate thread. If status_checker is provided,
        it should be a function that returns the current status as a string.
        """
        if status_checker:
            # Use StatusSpinner if a status checker is provided
            with StatusSpinner(message, status_checker):
                return func()
        else:
            # Use regular Spinner if no status checker is provided
            with Spinner(message):
                return func()

    # Async HTTP methods for Textual UI
    async def _get_async(
        self,
        path: str = "",
        params: dict | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        """Get a resource from the API (async)"""
        url = self._base_url if not path else f"{self._base_url}{path}"
        return await self._make_request_async("GET", url, json, params)

    async def _post_async(
        self,
        path: str = "",
        params: dict | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        """Post a resource to the API (async)"""
        url = self._base_url if not path else f"{self._base_url}{path}"
        return await self._make_request_async("POST", url, json, params)

    async def _patch_async(
        self,
        path: str = "",
        params: dict | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        """Patch a resource to the API (async)"""
        url = self._base_url if not path else f"{self._base_url}{path}"
        return await self._make_request_async("PATCH", url, json, params)

    async def _put_async(
        self,
        path: str = "",
        params: dict | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        """Put a resource to the API (async)"""
        url = self._base_url if not path else f"{self._base_url}{path}"
        return await self._make_request_async("PUT", url, json, params)

    async def _delete_async(
        self,
        path: str = "",
        params: dict | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        """Delete a resource from the API (async)"""
        url = self._base_url if not path else f"{self._base_url}{path}"
        return await self._make_request_async("DELETE", url, json, params)
