from typing import Dict, Optional, Any, Callable, Tuple
import httpx
from cli.config import config
from cli.api.utils import Spinner, StatusSpinner

class BaseAPI:
    def __init__(self, url_path: str):
        self._base_url = f"{config.api_base_url}/{config.api_version}/{url_path}"
        self.timeout = 300.0

    def _get_client(self) -> httpx.Client:
        """Get an HTTP client with authentication"""
        headers = {}
        if config.active_api_key:
            api_key = config.active_api_key_value
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            else:
                raise Exception("No API key set. Please set an API key with `lazycloud keys add`")

        return httpx.Client(timeout=self.timeout, headers=headers)

    def _make_request(
        self,
        method: str,
        url: str,
        json: Optional[Dict[str, Any]] = None,
        params: Optional[dict] = None,
    ) -> Any:
        """Make a request to the API"""
        client = self._get_client()
        try:
            response = client.request(method, url, json=json, params=params)
            response.raise_for_status()
            return response.json()

        except Exception as e:
            raise e

    def _get(
        self,
        path: str = "",
        params: Optional[dict] = None,
        json: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Get a resource from the API"""
        url = self._base_url if not path else f"{self._base_url}/{path}"
        return self._make_request("GET", url, json, params)

    def _post(
        self,
        path: str = "",
        params: Optional[dict] = None,
        json: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Post a resource to the API"""
        url = self._base_url if not path else f"{self._base_url}/{path}"
        return self._make_request("POST", url, json, params)

    def _put(
        self,
        path: str = "",
        params: Optional[dict] = None,
        json: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Put a resource to the API"""
        url = self._base_url if not path else f"{self._base_url}/{path}"
        return self._make_request("PUT", url, json, params)

    def _delete(
        self,
        path: str = "",
        params: Optional[dict] = None,
        json: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Delete a resource from the API"""
        url = self._base_url if not path else f"{self._base_url}/{path}"
        return self._make_request("DELETE", url, json, params)

    def _run_with_spinner(
        self,
        message: str,
        func: Callable,
        status_checker: Optional[Callable[[], str]] = None
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

